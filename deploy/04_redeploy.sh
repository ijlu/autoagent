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

# Stop bot during deploy
ssh "root@${SERVER}" "systemctl stop kalshi-bot.timer 2>/dev/null || true"

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
from bot.market_maker.core import mm_run
from bot.market_maker.family_caps import check_family_caps, is_family_blocked
from bot.learning.active_feedback import compute_active_feedback
from bot.scoring.market_scorer import score_market
assert kalshi_maker_fee(10, 50) == 5, 'Fee formula check failed'
print('bot/ imports OK — all new modules verified')
\""
echo "  bot/ OK"

# Increase systemd timeout (QA loop + API rate limits need more time)
echo "Updating systemd timeout to 180s..."
ssh "root@${SERVER}" "sed -i 's/TimeoutStartSec=120/TimeoutStartSec=180/' /etc/systemd/system/kalshi-bot.service && systemctl daemon-reload"

# Reset pipeline_health death spiral for previously-disabled sources
echo "Resetting pipeline health for disabled sources..."
ssh "root@${SERVER}" "sudo -u kalshi sqlite3 /home/kalshi/autoagent/kalshi_trades.db \"DELETE FROM pipeline_health WHERE source IN ('company_kpi','sensortower','series','clevfed','metaculus','polymarket','finnhub');\" 2>/dev/null || true"
echo "  Pipeline health reset — all sources will get a fresh start"

# Restart bot
ssh "root@${SERVER}" "systemctl start kalshi-bot.timer"
echo "Bot redeployed and restarted."
