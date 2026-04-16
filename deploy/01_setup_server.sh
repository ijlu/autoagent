#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi Bot — Server Setup Script
# Run this ON THE VPS after SSH-ing in as root
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "══════════════════════════════════════════════"
echo "  Kalshi Bot — Server Setup"
echo "══════════════════════════════════════════════"

# 1. Create bot user (non-root for security)
echo "[1/7] Creating bot user..."
if ! id "kalshi" &>/dev/null; then
    useradd -m -s /bin/bash kalshi
    echo "  Created user 'kalshi'"
else
    echo "  User 'kalshi' already exists"
fi

# 2. System packages
echo "[2/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv sqlite3 curl jq unattended-upgrades > /dev/null
echo "  Done"

# 3. Set up bot directory
echo "[3/7] Setting up bot directory..."
mkdir -p /home/kalshi/autoagent
chown -R kalshi:kalshi /home/kalshi/autoagent

# 4. Python dependencies
echo "[4/7] Installing Python packages..."
sudo -u kalshi pip3 install --user --break-system-packages \
    cryptography requests python-dotenv 2>/dev/null || \
sudo -u kalshi pip3 install --user \
    cryptography requests python-dotenv
echo "  Done"

# 5. Firewall — lock down everything except SSH
echo "[5/7] Configuring firewall..."
ufw --force reset > /dev/null 2>&1
ufw default deny incoming > /dev/null
ufw default allow outgoing > /dev/null
ufw allow ssh > /dev/null
ufw --force enable > /dev/null
echo "  Firewall enabled (SSH only)"

# 6. Set up auto-updates for security patches
echo "[6/7] Enabling automatic security updates..."
dpkg-reconfigure -f noninteractive unattended-upgrades > /dev/null 2>&1
echo "  Done"

# 7. Create systemd service (better than cron for 2-min intervals)
echo "[7/7] Creating systemd timer..."
cat > /etc/systemd/system/kalshi-bot.service << 'EOF'
[Unit]
Description=Kalshi Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=kalshi
Group=kalshi
WorkingDirectory=/home/kalshi/autoagent
ExecStart=/usr/bin/python3 /home/kalshi/autoagent/trade.py
StandardOutput=append:/home/kalshi/autoagent/cron.log
StandardError=append:/home/kalshi/autoagent/cron.log
TimeoutStartSec=120
# Restart protections
Nice=10
MemoryMax=512M
EOF

cat > /etc/systemd/system/kalshi-bot.timer << 'EOF'
[Unit]
Description=Run Kalshi Bot every 2 minutes

[Timer]
OnBootSec=30
OnUnitActiveSec=2min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

# Health check timer (every 30 min)
cat > /etc/systemd/system/kalshi-health.service << 'EOF'
[Unit]
Description=Kalshi Bot Health Check
After=network-online.target

[Service]
Type=oneshot
User=kalshi
Group=kalshi
WorkingDirectory=/home/kalshi/autoagent
ExecStart=/usr/bin/python3 /home/kalshi/autoagent/health_check.py
StandardOutput=append:/home/kalshi/autoagent/health.log
StandardError=append:/home/kalshi/autoagent/health.log
TimeoutStartSec=60
EOF

cat > /etc/systemd/system/kalshi-health.timer << 'EOF'
[Unit]
Description=Run Kalshi Health Check every 30 minutes

[Timer]
OnBootSec=60
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF

# Log rotation to prevent disk fill
cat > /etc/logrotate.d/kalshi-bot << 'EOF'
/home/kalshi/autoagent/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF

systemctl daemon-reload

echo ""
echo "══════════════════════════════════════════════"
echo "  Server setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Upload bot files (run 02_deploy.sh from your Mac)"
echo "  2. Upload your Kalshi private key"
echo "  3. Start the bot with: systemctl enable --now kalshi-bot.timer kalshi-health.timer"
echo "══════════════════════════════════════════════"
