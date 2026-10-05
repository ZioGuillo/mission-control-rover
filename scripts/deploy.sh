#!/usr/bin/env bash
# Deploy latest code to the robocar and restart the service.
# Usage: bash scripts/deploy.sh
# Override: ROBOCAR_HOST=ec2-user@192.168.1.93 bash scripts/deploy.sh
set -euo pipefail

ROBOCAR_HOST="${ROBOCAR_HOST:-ec2-user@192.168.1.93}"
REMOTE_DIR="/home/ec2-user/robocontrol"
SERVICE_NAME="robocontrol"

echo "=== robocar deploy ==="
echo "Target : $ROBOCAR_HOST"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ Syncing systemd service file"
scp "$SCRIPT_DIR/robocontrol.service" "$ROBOCAR_HOST:/tmp/robocontrol.service"

ssh "$ROBOCAR_HOST" bash << ENDSSH
set -e

echo "→ Installing service file"
sudo mv /tmp/robocontrol.service /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload

echo "→ Pulling latest code"
cd ${REMOTE_DIR}
git pull

echo "→ Restarting service"
sudo systemctl restart ${SERVICE_NAME}
sleep 3

if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "✓ ${SERVICE_NAME} is running"
    systemctl status ${SERVICE_NAME} --no-pager -l | tail -6
else
    echo "✗ Service failed — last 30 log lines:"
    journalctl -u ${SERVICE_NAME} -n 30 --no-pager
    exit 1
fi
ENDSSH

echo ""
echo "=== Deploy complete ==="
echo "  Logs : ssh $ROBOCAR_HOST journalctl -u $SERVICE_NAME -f"
