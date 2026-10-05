#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

sleep 10
cd "$SCRIPT_DIR"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
