#!/bin/bash
# Kalshi Trading Bot v3.11 — Local runner
# Add to crontab: */15 6-20 * * * cd ~/autoagent && ./run_bot.sh >> logs/bot.log 2>&1

set -a
source "$(dirname "$0")/.env"
set +a

# Override paths for local execution
export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi_private_key.pem"
export DB_PATH="$(dirname "$0")/kalshi_trades.db"
export REPORT_PATH="$(dirname "$0")/PERFORMANCE_REPORT.md"
export DIAGNOSTIC_REPORT_PATH="$(dirname "$0")/DIAGNOSTIC_REPORT.md"

# Ensure log directory exists
mkdir -p "$(dirname "$0")/logs"

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Kalshi Bot Run — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "════════════════════════════════════════════════════════"

cd "$(dirname "$0")"

# Force x86_64 architecture to match terminal where packages were installed
arch -x86_64 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 trade.py 2>&1

echo "[runner] Exit code: $?"
echo "[runner] DB size: $(du -h kalshi_trades.db 2>/dev/null | cut -f1)"
