#!/usr/bin/env bash
# Pull the latest code and restart the India ORB bot on the VPS.
#
# Run from the VPS:
#   bash ~/trading-bot/deploy/update_vps.sh
#
# Or from your Mac in one command (replace user/ip):
#   ssh user@YOUR_VPS_IP "bash trading-bot/deploy/update_vps.sh"
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="india-orb-bot"

echo "==> Pulling latest code..."
git -C "$REPO_DIR" pull origin main

echo "==> Installing/updating Python dependencies..."
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt" -q

echo "==> Reloading systemd and restarting service..."
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"

echo ""
echo "==> Service status:"
sudo systemctl status "$SERVICE" --no-pager -l

echo ""
echo "==> Last 20 log lines:"
journalctl -u "$SERVICE" --no-pager -n 20

echo ""
echo "Done. Run  journalctl -u $SERVICE -f  to tail live logs."
