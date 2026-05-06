#!/bin/bash
# Kalshi Bot Health Check — runs every hour
# Add to crontab: 0 * * * * cd ~/autoagent && ./run_health_check.sh >> logs/health.log 2>&1

set -a
source "$(dirname "$0")/.env"
set +a

export DB_PATH="$(dirname "$0")/kalshi_trades.db"
mkdir -p "$(dirname "$0")/logs"

cd "$(dirname "$0")"

echo ""
echo "── Health Check — $(date '+%Y-%m-%d %H:%M:%S %Z') ──"

arch -x86_64 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 health_check.py 2>&1
