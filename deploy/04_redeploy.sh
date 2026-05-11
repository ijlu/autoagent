#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi Bot — Quick Redeploy (code changes only, preserves DB + keys)
# Usage: ./04_redeploy.sh <server-ip>
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ./04_redeploy.sh <server-ip>"
    exit 1
fi

SERVER="$1"

# Derive BOT_DIR from the invocation cwd's git toplevel, not from a
# hardcoded $HOME/autoagent. Worktrees ($HOME/autoagent/.claude/worktrees/*)
# resolve to their own toplevel, so invoking this script from a worktree
# deploys the worktree's contents instead of silently shipping whatever
# happened to be in the main checkout.
#
# 2026-05-08 incident: a deploy invoked from a worktree silently shipped
# the main checkout (city-expansion branch + uncommitted WIP) because
# BOT_DIR was pinned to $HOME/autoagent. The same class of "silent state
# mismatch" bug as the 2026-05-01 .env overwrite documented below — both
# are caught by deriving from invocation context, not from process env.
if ! BOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "ERROR: deploy must be run from inside a git repo (cwd: $PWD)."
    echo "  cd into the checkout/worktree you want to deploy, then re-run."
    exit 1
fi

echo "Redeploying code to $SERVER (from: $BOT_DIR)..."

# Stop any running bot units during deploy (oneshot timer, weather daemon,
# or the new unified daemon — whichever happens to be live at the moment).
ssh "root@${SERVER}" "systemctl stop kalshi-bot.timer kalshi-bot.service kalshi-weather-daemon.service kalshi-daemon.service 2>/dev/null || true"

# Sanity check: prod must have a .env file before we proceed. We
# deliberately do NOT sync .env from local (see below) — it must be
# managed manually on the VPS so prod-only secrets (OPENMETEO_API_KEY,
# WEATHER_QUOTE_MAX_LST_HOUR, etc.) survive redeploys cleanly.
if ! ssh "root@${SERVER}" "[ -f /home/kalshi/autoagent/.env ]"; then
    echo "ERROR: /home/kalshi/autoagent/.env does not exist on $SERVER."
    echo "Create it manually (chmod 600) before redeploying. See:"
    echo "  reports/CROSS_BRACKET_CANARY_PROCEDURE.md (env_setup section)"
    exit 1
fi

# Sync Python files, bot/ package, tests/, context/, deploy scripts.
# .env is INTENTIONALLY EXCLUDED — see 2026-05-01 incident below.
#
# 2026-05-01: previously included .env in the rsync, which silently
# overwrote prod with whatever happened to be in local .env. That
# stripped OPENMETEO_API_KEY mid-day right after we paid for the
# commercial tier. .env is now prod-only; secrets stay where they
# were set + redeploys never touch them. The trade-off: when adding
# a new env var that the daemon needs, you must also `ssh` to prod
# and `echo VAR=value >> .env` (then restart). One-time cost per new
# env var, vs continuously losing prod secrets to local stale state.
rsync -avz --progress \
    --filter='- __pycache__/' \
    --filter='- *.pyc' \
    --filter='- .pytest_cache/' \
    --include='bot/' --include='bot/***' \
    --include='tests/' --include='tests/***' \
    --include='context/' --include='context/***' \
    --include='deploy/' --include='deploy/***' \
    --include='scripts/' --include='scripts/***' \
    --include='tools/' --include='tools/***' \
    --include='*.py' \
    --include='.gitignore' \
    --exclude='.env' \
    --exclude='*' \
    "$BOT_DIR/" "root@${SERVER}:/home/kalshi/autoagent/"

# Phase 0 (2026-04-16): remove the market-maker package and associated files.
# These were deleted locally but rsync (without --delete) leaves them on the VPS.
# Using surgical rm paths instead of rsync --delete to avoid accidentally nuking
# kalshi_trades.db or log files.
ssh "root@${SERVER}" "rm -rf /home/kalshi/autoagent/bot/market_maker \
    /home/kalshi/autoagent/bot/orchestrator.py \
    /home/kalshi/autoagent/bot/observability/opportunity_log.py \
    /home/kalshi/autoagent/bot/learning/threshold_tuner.py \
    /home/kalshi/autoagent/trade_v3_audit.py \
    /home/kalshi/autoagent/trade_audit_export.py \
    /home/kalshi/autoagent/trade_v1_backup.py \
    /home/kalshi/autoagent/trade_v2_backup.py \
    /home/kalshi/autoagent/agent.py \
    /home/kalshi/autoagent/agent-claude.py \
    /home/kalshi/autoagent/.kalshi_private_key.pem \
    /home/kalshi/autoagent/Users \
    /home/kalshi/autoagent/tests/test_family_caps.py \
    /home/kalshi/autoagent/tests/test_mm_opportunity_log.py \
    /home/kalshi/autoagent/tests/test_mm_postmortems.py \
    /home/kalshi/autoagent/tests/test_adverse_selection_defenses.py \
    /home/kalshi/autoagent/tests/test_threshold_tuner.py \
    /home/kalshi/autoagent/tests/test_mm_promotion_golden.py"

# Fix ownership + DB permissions
ssh "root@${SERVER}" "chown -R kalshi:kalshi /home/kalshi/autoagent && chmod 600 /home/kalshi/autoagent/.env && chmod 664 /home/kalshi/autoagent/kalshi_trades.db 2>/dev/null || true"

# Syntax check before restarting
echo "Syntax check..."
ssh "root@${SERVER}" "sudo -u kalshi python3 -c \"import py_compile; py_compile.compile('/home/kalshi/autoagent/trade.py', doraise=True)\""
echo "  trade.py OK"
echo "Module import check..."
ssh "root@${SERVER}" "cd /home/kalshi/autoagent && sudo -u kalshi python3 -c \"
from bot.core.money import kalshi_maker_fee, kalshi_taker_fee
from bot.config import HOST, compute_dynamic_sizing, SOURCE_MAX_HORIZON_DAYS
from bot.config import SC_ENABLED, SC_DRY_RUN, MM_MAX_DAYS_TO_EXPIRY, MAX_PORTFOLIO_EXPOSURE_RATIO
from bot.db import init_db
from bot.signals.ensemble import get_independent_estimate
from bot.signals.sources.metar_observations import get_metar_observation_estimate
from bot.signals.sources.deribit_vol import get_deribit_implied_prob
from bot.signals.sources.fedwatch import get_fedwatch_estimate
from bot.signals.sources.zq_futures import fetch_zq_fedwatch_probabilities
from bot.learning.active_feedback import compute_active_feedback
from bot.scoring.market_scorer import score_market
from bot.daemon.orchestrator import WeatherDaemon
from bot.daemon.metar_poller import METARPoller
from bot.daemon.smart_gates import evaluate_all_gates
from bot.daemon.weather_quoter import WeatherQuoter
from bot.daemon.stations import STATIONS
# Phase 1 daemon modules
from bot.daemon.locks import API_LOCK, PIPELINE_STATS_LOCK, DB_WRITE_LOCK
from bot.daemon.poller_base import Poller
from bot.daemon.scheduler import Scheduler
from bot.daemon.cycle_runner import CycleRunner
from bot.daemon.main import main as daemon_main
# T1.1 canonical station registry
from bot.daemon.stations import STATION_BY_SERIES, STATIONS
# T1.2 requote triggers + handler synthetic path
from bot.daemon.requote_triggers import (
    TimeDecayDriver, ForecastChangeDriver,
    REASON_METAR_CHANGE, REASON_TIME_DECAY, REASON_FORECAST_CHANGE,
    VALID_REASONS,
)
from bot.daemon.weather_handler import WeatherChangeHandler
# T2 bakeoff reporter
from bot.learning.bakeoff import render_bakeoff_report, compute_bakeoff
from bot.learning.mm_promotion import (
    evaluate_mm_promotion, evaluate_mm_kill_switch, evaluate_mm_graduation,
    _attribute_live_fills_to_shadow_rows,
)
from bot.config import (
    MM_CANARY_MIN_PNL_PER_FILL_CENTS,
    MM_GRADUATION_MIN_PAIRED_N,
    MM_GRADUATION_MIN_PNL_RATIO,
)
# T3.3 reader migration: regime detector + backtest now read fills from
# fills_ledger rather than the now-writer-less mm_processed_fills.
from bot.signals.regime import detect_regime
from bot.learning.alpha_log import log_decision, DecisionType, DecisionOutcome
# T3.1 canonical fills ledger + dual-run validator
from bot.daemon.fills_writer import FillsWriter
from bot.learning.fills_validator import (
    compare_last_n_days, format_report, ValidationReport, Divergence, TickerSideStats,
)
# Phase 2 weather expansion
from bot.signals.sources.nws_point import get_nws_point_estimate
from bot.signals.sources.ndfd_nbm import get_nbm_estimate
from bot.signals.sources.hrrr import get_hrrr_estimate
from bot.signals.sources.madis import get_madis_estimate
from bot.signals.sources.afd import get_afd_estimate
from bot.signals.weather_ensemble import predict as weather_ensemble_predict
# Phase 3 economics expansion
from bot.signals.sources.adp_nfp import get_adp_estimate
from bot.signals.sources.gdpnow import get_gdpnow_estimate
from bot.signals.sources.commodity_futures import get_commodity_cpi_estimate
from bot.signals.family_routers import route_family
assert kalshi_maker_fee(10, 50) == 5, 'Fee formula check failed'
assert len(STATIONS) >= 3, 'Station config check failed'
# Verify METARPoller picked up Poller ABC and its 30s default interval
assert METARPoller().interval_s == 30.0, 'METARPoller interval regression'
# Verify family router is prefix-registered
assert route_family('KXFED-26JUL', {}) is None, 'router should skip unknown prefixes'
# T1.1 — canonical registry: NY primary must be KNYC not KJFK, KJFK demoted to backup
assert STATION_BY_SERIES['KXHIGHNY'].icao == 'KNYC', 'T1.1 registry regression: KXHIGHNY primary not KNYC'
assert 'KJFK' in STATION_BY_SERIES['KXHIGHNY'].backups, 'T1.1 registry regression: KJFK not in KXHIGHNY.backups'
assert 'KJFK' not in STATIONS, 'T1.1 registry regression: KJFK leaked back into primary STATIONS map'
# T1.2 — valid trigger-reason frozenset locked to 3 labels
assert VALID_REASONS == frozenset({REASON_METAR_CHANGE, REASON_TIME_DECAY, REASON_FORECAST_CHANGE}), \
    'T1.2 trigger-reason contract regression'
# T3.1 — FillsWriter must expose ingest_page + sync_since; column tuple is
# the canonical schema, guard against accidental rename.
assert hasattr(FillsWriter, 'ingest_page'), 'T3.1 regression: FillsWriter.ingest_page missing'
assert hasattr(FillsWriter, 'sync_since'), 'T3.1 regression: FillsWriter.sync_since missing'
assert 'trade_id' in FillsWriter._COLUMNS, 'T3.1 regression: FillsWriter._COLUMNS lost trade_id PK'
# T3.1 validator surface — public API frozen until T3.3 reader migration
_report = ValidationReport(n_days=7, since_unix=0.0, reference_name='x',
                            ledger_contracts=0, reference_contracts=0)
assert _report.is_clean and not _report.is_meaningful, 'T3.1 validator semantics regression'
# B+D (2026-04-21) — two-gate MM promotion + canary graduation. The
# Apr-17 _safe_cents bug auto-promoted two families on fake fills; if
# these knobs or the graduation surface go missing, the single-criterion
# rule sneaks back in.
assert MM_CANARY_MIN_PNL_PER_FILL_CENTS > 0, 'B+D regression: canary P&L floor must be positive'
assert MM_GRADUATION_MIN_PAIRED_N >= 5, 'B+D regression: graduation N too low'  # 2026-04-26: floor relaxed from 10 to 5 alongside default 8 to compress canary window
assert 0.0 < MM_GRADUATION_MIN_PNL_RATIO <= 1.0, 'B+D regression: graduation ratio out of range'
assert callable(evaluate_mm_graduation), 'B+D regression: evaluate_mm_graduation missing'
# T3.3 (2026-04-21) — reader migration from mm_processed_fills to
# fills_ledger, plus the live_pnl_cents annotator that makes the
# graduation gate actually fireable. Without the attribution helper the
# gate's live_pnl_cents IS NOT NULL filter matches zero rows forever.
assert callable(_attribute_live_fills_to_shadow_rows), 'T3.3 regression: live-fill attributor missing'
assert callable(detect_regime), 'T3.3 regression: regime detector import failed (fills_ledger switch)'
# Post-mortem follow-on #2 — shadow data-integrity monitor. Would have
# caught the Apr-17 zero-book corruption within minutes instead of 4 days.
from bot.daemon.shadow_integrity import (
    check_shadow_data_integrity,
    run_shadow_integrity_check,
    IntegrityFinding,
    MIN_ROWS_FOR_SIGNAL,
    DEFAULT_WINDOW_S,
)
assert callable(check_shadow_data_integrity), 'shadow_integrity regression: check function missing'
assert MIN_ROWS_FOR_SIGNAL >= 10, 'shadow_integrity regression: signal threshold too low (false-positive risk)'
assert DEFAULT_WINDOW_S >= 3600, 'shadow_integrity regression: window too short'
# 2026-04-22 — catalog-driven settlement back-fill poller. Fixes the
# portfolio-vs-catalog gap that was starving Platt calibration for weeks.
from bot.learning.settlement_backfill import (
    backfill_from_catalog, fetch_settled_markets, _parse_close_ts,
    _distinct_unsettled_series, DEFAULT_MAX_PAGES,
)
assert callable(backfill_from_catalog), 'settlement_backfill regression: entrypoint missing'
assert DEFAULT_MAX_PAGES >= 5, 'settlement_backfill regression: pagination cap too low'
assert _parse_close_ts('2026-04-21T20:00:00Z') is not None, 'settlement_backfill regression: Zulu ISO parse broken'
# 2026-04-22 — shadow→calibration bridge. Converts settled weather_mm_shadow
# rows into calibration training data so the Platt fit has per-family signal
# without waiting for fresh alpha_backtest accumulation (Platt starvation
# root cause — 27K+ shadow rows settled but never reached the fitter).
from bot.learning.shadow_calibration_bridge import (
    bridge_shadow_to_calibration, WATERMARK_KEY, SOURCE_DESC,
)
assert callable(bridge_shadow_to_calibration), 'shadow_cal_bridge regression: entrypoint missing'
assert WATERMARK_KEY == 'shadow_cal_bridge_watermark', 'shadow_cal_bridge regression: watermark key changed (would silently reset dedup on deploy)'
assert SOURCE_DESC == 'weather_mm_shadow', 'shadow_cal_bridge regression: source_desc changed (breaks downstream audit filters)'
# MOS bias fitter — wired into daemon via _run_mos_materializer so warm-bias corrections reach kv_cache
from bot.learning.weather_mos_materializer import fit_and_persist_mos_bias
assert callable(fit_and_persist_mos_bias), 'mos_fitter regression: fit_and_persist_mos_bias missing'
print('bot/ imports OK — Phase 2 weather expansion + Phase 3 econ sources + T1.1/T1.2/T3.1/T3.3/B+D/shadow_integrity/settlement_backfill/shadow_cal_bridge/mos_fitter wired')
\""
echo "  bot/ OK"

# Install Phase 1 daemon unit file and switch from oneshot → daemon
echo "Installing kalshi-daemon.service..."
ssh "root@${SERVER}" "cp /home/kalshi/autoagent/deploy/kalshi-daemon.service /etc/systemd/system/ && systemctl daemon-reload"

# Install cachetools (required by bot.api TTLCache). --break-system-packages
# needed under PEP 668 on Ubuntu 23+. Idempotent; no-op if already installed.
echo "Ensuring Python deps..."
ssh "root@${SERVER}" "python3 -m pip install --break-system-packages --quiet cachetools >/dev/null 2>&1 || true"

# Reset pipeline_health death spiral for previously-disabled sources
echo "Resetting pipeline health for disabled sources..."
ssh "root@${SERVER}" "sudo -u kalshi sqlite3 /home/kalshi/autoagent/kalshi_trades.db \"DELETE FROM pipeline_health WHERE source IN ('company_kpi','sensortower','series','clevfed','metaculus','polymarket','finnhub');\" 2>/dev/null || true"
echo "  Pipeline health reset — all sources will get a fresh start"

# Cutover: disable the old oneshot + weather daemon, start the unified daemon.
echo "Cutting over to persistent daemon..."
ssh "root@${SERVER}" "systemctl disable --now kalshi-bot.timer 2>/dev/null || true"
ssh "root@${SERVER}" "systemctl disable --now kalshi-weather-daemon.service 2>/dev/null || true"
ssh "root@${SERVER}" "systemctl enable --now kalshi-daemon.service"

# Brief health check
echo "Waiting 10s for first cycle..."
sleep 10
ssh "root@${SERVER}" "systemctl is-active kalshi-daemon.service" && echo "  kalshi-daemon: active"
ssh "root@${SERVER}" "tail -20 /home/kalshi/autoagent/daemon.log 2>/dev/null || echo '(daemon.log not yet populated)'"
echo ""
echo "Bot redeployed. Watch logs with:"
echo "  ssh root@${SERVER} 'tail -f /home/kalshi/autoagent/daemon.log'"
