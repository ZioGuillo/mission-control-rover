#!/usr/bin/env bash
# Install (or --uninstall) the Houston launchd services: whisper-server + houston.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
AGENTS="$HOME/Library/LaunchAgents"
MODEL="$HOME/Library/Caches/houston/ggml-large-v3-turbo.bin"
LABELS=(local.missioncontrol.whisper local.missioncontrol.houston)
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    for label in "${LABELS[@]}"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        rm -f "$AGENTS/$label.plist"
    done
    echo "Houston services removed (ollama and the models are left alone)."
    exit 0
fi

die() { echo "error: $*" >&2; exit 1; }

command -v ollama >/dev/null || die "ollama not found (brew install ollama)"
[[ -x /opt/homebrew/bin/whisper-server ]] || die "whisper-server not found (brew install whisper-cpp)"
command -v ffmpeg >/dev/null || die "ffmpeg not found (brew install ffmpeg)"
[[ -f "$MODEL" ]] || die "whisper model missing: $MODEL"
[[ -x "$HERE/.venv/bin/python" ]] || die "houston/.venv missing (see houston/README.md)"

# Ollama stays on its default, localhost only: never set OLLAMA_HOST.
brew services start ollama
for _ in $(seq 1 30); do
    ollama list >/dev/null 2>&1 && break
    sleep 1
done
installed="$(ollama list)"
for model in gpt-oss:120b qwen3.8:27b; do
    grep -q "^$model[[:space:]]" <<<"$installed" || ollama pull "$model"
done

mkdir -p "$AGENTS" "$HOME/Library/Logs"
for label in "${LABELS[@]}"; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" \
        "$HERE/launchd/$label.plist" > "$AGENTS/$label.plist"
    plutil -lint "$AGENTS/$label.plist" >/dev/null
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
done
echo "Houston installed. Logs: ~/Library/Logs/houston.log"
