#!/usr/bin/env bash
# fan-mode.sh — switch the Jetson fan mode (see scripts/jetson-fan.sh)
#
#   scripts/fan-mode.sh 1   auto: off while cool, spins up as it heats (default)
#   scripts/fan-mode.sh 2   always: always spinning gently, faster when hot
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

case "${1:-}" in
    1|auto)   mode=auto ;;
    2|always) mode=always ;;
    *)
        echo "Usage: $0 1|2"
        echo "  1 = auto   (off while cool, spins up as it heats — default)"
        echo "  2 = always (always spinning gently, faster when hot)"
        exit 1 ;;
esac

if grep -q '^FAN_MODE=' "$ENV_FILE"; then
    sed -i "s/^FAN_MODE=.*/FAN_MODE=$mode/" "$ENV_FILE"
else
    printf '\nFAN_MODE=%s\n' "$mode" >> "$ENV_FILE"
fi
sudo systemctl restart jetson-fan.service
echo "Fan mode: $mode"
systemctl status jetson-fan.service --no-pager -n 3 | tail -3
