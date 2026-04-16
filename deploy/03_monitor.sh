#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi Bot — Quick Monitor (run from your Mac)
# Usage: ./03_monitor.sh <server-ip>
# ═══════════════════════════════════════════════════════════════════════════════

if [ -z "${1:-}" ]; then
    echo "Usage: ./03_monitor.sh <server-ip>"
    exit 1
fi

SERVER="$1"

echo "══════════════════════════════════════════════"
echo "  Kalshi Bot Status — $SERVER"
echo "══════════════════════════════════════════════"

ssh "root@${SERVER}" << 'REMOTE'
echo ""
echo "── Timer Status ──"
systemctl status kalshi-bot.timer --no-pager 2>/dev/null | head -5
echo ""
echo "── Last 3 Runs ──"
journalctl -u kalshi-bot.service --no-pager -n 3 --output short-iso 2>/dev/null
echo ""
echo "── Recent Log (last 30 lines) ──"
sudo -u kalshi tail -30 /home/kalshi/autoagent/cron.log 2>/dev/null || echo "(no log yet)"
echo ""
echo "── Health Check ──"
sudo -u kalshi tail -5 /home/kalshi/autoagent/health.log 2>/dev/null || echo "(no health log yet)"
echo ""
echo "── Disk & Memory ──"
df -h / | tail -1
free -h | head -2
echo ""
echo "── DB Size ──"
ls -lh /home/kalshi/autoagent/kalshi_trades.db 2>/dev/null || echo "(no DB yet)"
REMOTE
