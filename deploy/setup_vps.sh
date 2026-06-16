#!/usr/bin/env bash
# Bootstrap script for the India ORB bot on a fresh Ubuntu VPS (DigitalOcean droplet).
#
# Run this AFTER cloning the repo on the server:
#   git clone <repo-url> trading-bot
#   cd trading-bot
#   bash deploy/setup_vps.sh
#
# What it does:
#   - installs Python, venv, firewall tooling
#   - creates a venv and installs requirements
#   - locks down the firewall to SSH-only (bot only makes outbound calls)
#   - installs the systemd service so the bot survives reboots/crashes
#
# It does NOT create your .env file — do that manually (see step printed at the end).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="india-orb-bot"

echo "==> Updating system packages"
sudo apt-get update -y
sudo apt-get upgrade -y

echo "==> Installing Python, venv, firewall"
sudo apt-get install -y python3 python3-venv python3-pip ufw

echo "==> Creating virtualenv in $REPO_DIR/venv"
python3 -m venv "$REPO_DIR/venv"
"$REPO_DIR/venv/bin/pip" install --upgrade pip
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> Locking down firewall (SSH only — bot makes outbound calls only)"
sudo ufw allow OpenSSH
sudo ufw --force enable

echo "==> Setting server timezone to Asia/Kolkata (IST) — matches NSE hours, easier log reading"
sudo timedatectl set-timezone Asia/Kolkata

echo "==> Installing systemd service + timer"
sudo sed \
    -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__USER__|$(whoami)|g" \
    "$REPO_DIR/deploy/india-orb-bot.service" \
    | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null
sudo cp "$REPO_DIR/deploy/india-orb-bot.timer" "/etc/systemd/system/${SERVICE_NAME}.timer"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.timer"
sudo systemctl start "${SERVICE_NAME}.timer"

echo ""
echo "=================================================================="
echo "  Setup complete. Before starting the bot:"
echo ""
echo "  1. Create your .env file (NEVER commit this):"
echo "       nano $REPO_DIR/.env"
echo ""
echo "     Add these lines with your real AngelOne credentials:"
echo "       ANGELONE_API_KEY=..."
echo "       ANGELONE_CLIENT_CODE=..."
echo "       ANGELONE_PASSWORD=..."
echo "       ANGELONE_TOTP_SECRET=..."
echo ""
echo "  2. Register this server's public IP with AngelOne:"
echo "       curl -s ifconfig.me"
echo ""
echo "  3. Start the bot in DRY RUN first to confirm it connects:"
echo "       cd $REPO_DIR && source venv/bin/activate"
echo "       python india_orb_bot.py --once --dry-run"
echo ""
echo "  4. Once confirmed, edit the installed service to remove --dry-run:"
echo "       sudo nano /etc/systemd/system/${SERVICE_NAME}.service"
echo "       sudo systemctl daemon-reload"
echo ""
echo "  The timer fires automatically every weekday at 08:50 IST — the bot"
echo "  waits for market open itself and exits cleanly after close, so"
echo "  there's nothing else to schedule. To run it manually right now:"
echo "       sudo systemctl start $SERVICE_NAME"
echo "       sudo systemctl status $SERVICE_NAME"
echo "       journalctl -u $SERVICE_NAME -f       # live logs"
echo ""
echo "  Check timer schedule:"
echo "       systemctl list-timers ${SERVICE_NAME}.timer"
echo "=================================================================="
