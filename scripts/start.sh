#!/usr/bin/env bash
# Start (or restart) the RoboControl server in the background.
# Safe to call repeatedly — kills any running instance first.
# Usage: bash scripts/start.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

pkill -f uvicorn 2>/dev/null && sleep 1 || true

cd "$REPO_DIR"
nohup venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  >robocontrol.log 2>&1 &

echo "Started PID $!"
