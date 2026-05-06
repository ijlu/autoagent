#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi Weather Daemon — Deploy & Start
# Usage: ./05_deploy_weather_daemon.sh <server-ip> [--dry-run]
#
# Deploys the weather daemon alongside the existing 2-minute oneshot bot.
# The daemon handles weather markets (KXHIGH*, KXHMONTHRANGE, KXHURR).
# The oneshot bot handles everything else (KXFED, KXGDP, KXCPI, etc.).
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ./05_deploy_weather_daemon.sh <server-ip> [--dry-run]"
    exit 1
fi

SERVER="$1"
DRY_RUN="${2:-}"
BOT_DIR="$HOME/autoagent"

echo "═══════════════════════════════════════════════════════════"
echo "  Deploying Weather Daemon to $SERVER"
echo "═══════════════════════════════════════════════════════════"

# Step 1: Sync code (same as 04_redeploy.sh)
echo ""
echo "Step 1: Syncing code..."
rsync -avz --progress \
    --filter='- __pycache__/' \
    --filter='- *.pyc' \
    --filter='- .pytest_cache/' \
    --include='bot/' --include='bot/***' \
    --include='tests/' --include='tests/***' \
    --include='deploy/' --include='deploy/***' \
    --include='*.py' \
    --include='.env' \
    --exclude='*' \
    "$BOT_DIR/" "root@${SERVER}:/home/kalshi/autoagent/"

# Fix .env key path for server
ssh "root@${SERVER}" "sed -i 's|/Users/jlu/.kalshi_private_key.pem|/home/kalshi/.kalshi_private_key.pem|' /home/kalshi/autoagent/.env"
ssh "root@${SERVER}" "chown -R kalshi:kalshi /home/kalshi/autoagent && chmod 600 /home/kalshi/autoagent/.env"

# Step 2: Verify imports
echo ""
echo "Step 2: Verifying daemon imports..."
ssh "root@${SERVER}" "cd /home/kalshi/autoagent && sudo -u kalshi python3 -c \"
from bot.daemon.orchestrator import WeatherDaemon
from bot.daemon.metar_poller import METARPoller
from bot.daemon.smart_gates import evaluate_all_gates
from bot.daemon.weather_quoter import WeatherQuoter
from bot.daemon.stations import STATIONS, ALL_STATION_IDS
print(f'Daemon imports OK — {len(STATIONS)} stations configured')
print(f'Station IDs: {ALL_STATION_IDS}')
\""
echo "  ✓ Imports verified"

# Step 3: Install systemd service
echo ""
echo "Step 3: Installing systemd service..."
ssh "root@${SERVER}" "cp /home/kalshi/autoagent/deploy/kalshi-weather-daemon.service /etc/systemd/system/ && systemctl daemon-reload"
echo "  ✓ Service installed"

# Step 4: Start the daemon
echo ""
if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "Step 4: Starting daemon in DRY RUN mode..."
    # Override ExecStart for dry-run
    ssh "root@${SERVER}" "
        mkdir -p /etc/systemd/system/kalshi-weather-daemon.service.d
        cat > /etc/systemd/system/kalshi-weather-daemon.service.d/dry-run.conf << 'CONF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m bot.daemon.orchestrator --poll-interval 30 --dry-run --log-level DEBUG
CONF
        systemctl daemon-reload
        systemctl restart kalshi-weather-daemon
    "
    echo "  ✓ Daemon started (DRY RUN)"
else
    echo "Step 4: Starting daemon in LIVE mode..."
    # Remove any dry-run override
    ssh "root@${SERVER}" "rm -f /etc/systemd/system/kalshi-weather-daemon.service.d/dry-run.conf 2>/dev/null; systemctl daemon-reload"
    ssh "root@${SERVER}" "systemctl enable kalshi-weather-daemon && systemctl restart kalshi-weather-daemon"
    echo "  ✓ Daemon started (LIVE)"
fi

# Step 5: Verify it's running
echo ""
echo "Step 5: Verifying daemon status..."
sleep 3
ssh "root@${SERVER}" "systemctl is-active kalshi-weather-daemon && echo '  ✓ Daemon is running' || echo '  ✗ Daemon failed to start'"

# Show initial logs
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Initial daemon output:"
echo "═══════════════════════════════════════════════════════════"
ssh "root@${SERVER}" "tail -20 /home/kalshi/autoagent/weather_daemon.log 2>/dev/null || journalctl -u kalshi-weather-daemon -n 20 --no-pager"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Deploy complete!"
echo ""
echo "  Monitor: ssh kalshi@$SERVER 'tail -f ~/autoagent/weather_daemon.log'"
echo "  Status:  ssh root@$SERVER 'systemctl status kalshi-weather-daemon'"
echo "  Logs:    ssh root@$SERVER 'journalctl -u kalshi-weather-daemon -f'"
echo "═══════════════════════════════════════════════════════════"
