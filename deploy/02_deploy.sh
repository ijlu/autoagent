#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi Bot — Deploy from Mac to VPS
# Run this FROM YOUR MAC after the server is set up
# Usage: ./02_deploy.sh <server-ip>
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: ./02_deploy.sh <server-ip>"
    echo "Example: ./02_deploy.sh 167.99.123.45"
    exit 1
fi

SERVER="$1"
BOT_DIR="$HOME/autoagent"
REMOTE_DIR="/home/kalshi/autoagent"

echo "══════════════════════════════════════════════"
echo "  Deploying Kalshi Bot to $SERVER"
echo "══════════════════════════════════════════════"

# 1. Sync bot files (excluding secrets and DB)
echo "[1/4] Uploading bot code..."
rsync -avz --progress \
    --exclude '.env' \
    --exclude '*.db' \
    --exclude '*.db-journal' \
    --exclude '__pycache__' \
    --exclude 'deploy/' \
    --exclude '*.docx' \
    --exclude '*.backup' \
    --exclude 'trade_v*_backup.py' \
    "$BOT_DIR/" "root@${SERVER}:${REMOTE_DIR}/"

# 2. Upload .env with corrected key path
echo "[2/4] Uploading config..."
# Create server-specific .env (key path changes from Mac to Linux)
sed 's|/Users/jlu/.kalshi_private_key.pem|/home/kalshi/.kalshi_private_key.pem|' \
    "$BOT_DIR/.env" | \
    ssh "root@${SERVER}" "cat > ${REMOTE_DIR}/.env"

# 3. Upload Kalshi private key securely
echo "[3/4] Uploading Kalshi private key..."
if [ -f "$HOME/.kalshi_private_key.pem" ]; then
    scp "$HOME/.kalshi_private_key.pem" "root@${SERVER}:/home/kalshi/.kalshi_private_key.pem"
    ssh "root@${SERVER}" "chmod 600 /home/kalshi/.kalshi_private_key.pem && chown kalshi:kalshi /home/kalshi/.kalshi_private_key.pem"
    echo "  Key uploaded and secured (600 permissions)"
else
    echo "  WARNING: $HOME/.kalshi_private_key.pem not found!"
    echo "  You'll need to manually upload your Kalshi private key to:"
    echo "  /home/kalshi/.kalshi_private_key.pem on the server"
fi

# 4. Fix ownership and permissions
echo "[4/4] Setting permissions..."
ssh "root@${SERVER}" << 'REMOTE'
chown -R kalshi:kalshi /home/kalshi/autoagent
chmod 600 /home/kalshi/autoagent/.env
# Ensure DB is writable
touch /home/kalshi/autoagent/kalshi_trades.db
chown kalshi:kalshi /home/kalshi/autoagent/kalshi_trades.db
REMOTE

echo ""
echo "══════════════════════════════════════════════"
echo "  Deploy complete!"
echo ""
echo "  To start the bot, SSH in and run:"
echo "    ssh root@${SERVER}"
echo "    systemctl enable --now kalshi-bot.timer kalshi-health.timer"
echo ""
echo "  To check status:"
echo "    ssh root@${SERVER} 'systemctl status kalshi-bot.timer'"
echo "    ssh root@${SERVER} 'sudo -u kalshi tail -50 /home/kalshi/autoagent/cron.log'"
echo ""
echo "  To stop the bot:"
echo "    ssh root@${SERVER} 'systemctl stop kalshi-bot.timer'"
echo "══════════════════════════════════════════════"
