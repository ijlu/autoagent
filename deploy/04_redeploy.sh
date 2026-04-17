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
BOT_DIR="$HOME/autoagent"

echo "Redeploying code to $SERVER..."

# Stop any running bot units during deploy (oneshot timer, weather daemon,
# or the new unified daemon — whichever happens to be live at the moment).
ssh "root@${SERVER}" "systemctl stop kalshi-bot.timer kalshi-bot.service kalshi-weather-daemon.service kalshi-daemon.service 2>/dev/null || true"

# Sync Python files, bot/ package, tests/, context/, and .env
# Use --filter rules: include dirs first, then files, then exclude rest
rsync -avz --progress \
    --filter='- __pycache__/' \
    --filter='- *.pyc' \
    --filter='- .pytest_cache/' \
    --include='bot/' --include='bot/***' \
    --include='tests/' --include='tests/***' \
    --include='context/' --include='context/***' \
    --include='deploy/' --include='deploy/***' \
    --include='*.py' \
    --include='.env' \
    --exclude='*' \
    "$BOT_DIR/" "root@${SERVER}:/home/kalshi/autoagent/"

# Fix .env key path for server
ssh "root@${SERVER}" "sed -i 's|/Users/jlu/.kalshi_private_key.pem|/home/kalshi/.kalshi_private_key.pem|' /home/kalshi/autoagent/.env"

# Phase 0 (2026-04-16): remove the market-maker package and associated files.
# These were deleted locally but rsync (without --delete) leaves them on the VPS.
# Using surgical rm paths instead of rsync --delete to avoid accidentally nuking
# kalshi_trades.db or log files.
ssh "root@${SERVER}" "rm -rf /home/kalshi/autoagent/bot/market_maker \
    /home/kalshi/autoagent/bot/orchestrator.py \
    /home/kalshi/autoagent/tests/test_family_caps.py \
    /home/kalshi/autoagent/tests/test_mm_opportunity_log.py \
    /home/kalshi/autoagent/tests/test_mm_postmortems.py \
    /home/kalshi/autoagent/tests/test_adverse_selection_defenses.py"

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
print('bot/ imports OK — Phase 2 weather expansion + Phase 3 econ sources wired')
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
