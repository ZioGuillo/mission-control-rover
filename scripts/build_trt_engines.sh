#!/usr/bin/env bash
# Build the TensorRT engines that move ML detection onto the Jetson's GPU.
#
# Run on the Jetson itself (once, and again after retraining a model or
# upgrading JetPack): an engine only runs on the GPU + TensorRT version it
# was built with, so engines aren't in git. ~10 minutes per model on a
# Jetson Nano; the app keeps detecting on the CPU meanwhile.
#
#   scripts/build_trt_engines.sh            # builds missing / outdated engines
#   scripts/build_trt_engines.sh --force    # rebuilds all
#
# Then restart the app — app/hardware/ml_driver.py picks the GPU up by
# itself (ML_BACKEND=auto). Engines go to $DATA_DIR/trt (default
# ~/.local/share/robocar/trt).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TRTEXEC=/usr/src/tensorrt/bin/trtexec
DATA_DIR="${DATA_DIR:-$HOME/.local/share/robocar}"
if [ -f "$REPO_DIR/.env" ]; then
    env_dir=$(grep -E '^DATA_DIR=' "$REPO_DIR/.env" | tail -1 | cut -d= -f2- || true)
    [ -n "${env_dir:-}" ] && DATA_DIR="$env_dir"
fi
OUT="$DATA_DIR/trt"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ ! -x "$TRTEXEC" ]; then
    echo "trtexec not found at $TRTEXEC — this only runs on a Jetson with JetPack's TensorRT." >&2
    exit 1
fi
mkdir -p "$OUT"

built=0
for onnx in "$REPO_DIR"/app/models/*.onnx; do
    name=$(basename "$onnx" .onnx)
    engine="$OUT/$name.engine"
    if [ "$FORCE" = 0 ] && [ -f "$engine" ] && [ "$engine" -nt "$onnx" ]; then
        echo "✓ $name: engine up to date"
        continue
    fi
    echo "→ $name: building FP16 engine (≈10 min)…"
    start=$(date +%s)
    # build into a temp file: a half-written engine must never be loaded
    if "$TRTEXEC" --onnx="$onnx" --saveEngine="$engine.tmp" --fp16 --workspace=1024 \
            > "$OUT/$name.build.log" 2>&1; then
        mv "$engine.tmp" "$engine"
        ms=$(grep -oE "GPU Compute Time: min = [0-9.]+ ms, max = [0-9.]+ ms, mean = [0-9.]+" "$OUT/$name.build.log" \
             | grep -oE "[0-9.]+$" || echo "?")
        echo "  done in $(( $(date +%s) - start )) s — ${ms} ms per frame on the GPU"
        built=$((built + 1))
    else
        rm -f "$engine.tmp"
        echo "  FAILED — see $OUT/$name.build.log" >&2
        exit 1
    fi
done

if [ "$built" -gt 0 ]; then
    echo ""
    echo "Restart the app to use them:  sudo systemctl restart robocontrol"
fi
