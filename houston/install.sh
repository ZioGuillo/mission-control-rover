#!/usr/bin/env bash
# Install Houston as launchd services (whisper-server + houston), run from a copy OUTSIDE iCloud.
#   install.sh                     deploy runtime copy + venv, (re)load both jobs
#   install.sh --uninstall [--purge]   remove the jobs (--purge also removes the runtime copy and venv)
#   install.sh --render-only DIR   only render the two plists into DIR (no checks, nothing installed)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
SUPPORT="$HOME/Library/Application Support/houston"
RUNTIME="$SUPPORT/runtime"
VENV="$SUPPORT/venv"
AGENTS="$HOME/Library/LaunchAgents"
MODEL="$HOME/Library/Caches/houston/ggml-large-v3-turbo.bin"  # the one place the Whisper model is named
LABELS=(local.missioncontrol.whisper local.missioncontrol.houston)
DOMAIN="gui/$(id -u)"
LOGS="$HOME/Library/Logs"
LOG_MAX_BYTES=$((10 * 1024 * 1024))

die() { echo "error: $*" >&2; exit 1; }
usage() {
    echo "usage: install.sh | install.sh --uninstall [--purge] | install.sh --render-only DIR" >&2
    exit 2
}

# Check every argument before doing anything.
MODE=install PURGE= OUT=
case "$#:${1:-}" in
    0:) ;;
    1:--uninstall) MODE=uninstall ;;
    2:--uninstall) [[ "$2" == "--purge" ]] || usage; MODE=uninstall PURGE=1 ;;
    2:--render-only) [[ -d "$2" ]] || usage; MODE=render OUT="$2" ;;
    *) usage ;;
esac

# render_plists DIR: fill the templates with python's plistlib (safe for any characters in paths).
render_plists() {
    python3 - "$HERE/launchd" "$1" "$RUNTIME" "$VENV" "$HOME" "$MODEL" <<'PY'
import plistlib, sys
src, dst, runtime, venv, home, model = sys.argv[1:]
subs = {"@RUNTIME@": runtime, "@VENV@": venv, "@HOME@": home, "@MODEL@": model}

def fill(v):
    if isinstance(v, str):
        for k, r in subs.items():
            v = v.replace(k, r)
        return v
    if isinstance(v, list):
        return [fill(x) for x in v]
    if isinstance(v, dict):
        return {k: fill(x) for k, x in v.items()}
    return v

for label in ("local.missioncontrol.whisper", "local.missioncontrol.houston"):
    with open(f"{src}/{label}.plist", "rb") as f:
        data = fill(plistlib.load(f))
    with open(f"{dst}/{label}.plist", "wb") as f:
        plistlib.dump(data, f)
PY
}

if [[ "$MODE" == render ]]; then
    render_plists "$OUT"
    exit 0
fi

if [[ "$MODE" == uninstall ]]; then
    for label in "${LABELS[@]}"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        rm -f "$AGENTS/$label.plist"
    done
    if [[ -n "$PURGE" ]]; then
        rm -rf "$RUNTIME" "$VENV"
        echo "Runtime copy and venv removed."
    fi
    echo "Houston services removed (ollama and the models are left alone)."
    exit 0
fi

command -v brew >/dev/null || die "brew not found (https://brew.sh)"
command -v ollama >/dev/null || die "ollama not found (brew install ollama)"
[[ -x /opt/homebrew/bin/whisper-server ]] || die "whisper-server not found (brew install whisper-cpp)"
command -v ffmpeg >/dev/null || die "ffmpeg not found (brew install ffmpeg)"
command -v uv >/dev/null || die "uv not found (brew install uv)"
[[ -f "$MODEL" ]] || die "whisper model missing: $MODEL"

# Ollama stays on its default, localhost only: never set OLLAMA_HOST.
brew services start ollama
ready=
for _ in $(seq 1 30); do
    if ollama list >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
[[ -n "$ready" ]] || die "ollama did not come up within 30 s"
installed="$(ollama list)"
for model in gpt-oss:120b qwen3.8:27b; do
    grep -q "^$model[[:space:]]" <<<"$installed" || ollama pull "$model"
done

# Runtime copy outside iCloud (LaunchAgents may be denied there, and iCloud can evict files).
mkdir -p "$RUNTIME/houston" "$RUNTIME/app/audio" "$LOGS" "$AGENTS"
rsync -a --delete --exclude .venv --exclude tests --exclude __pycache__ \
    "$HERE/" "$RUNTIME/houston/"
rsync -a "$REPO/app/audio/quindar_in.wav" "$REPO/app/audio/quindar_out.wav" "$RUNTIME/app/audio/"
[[ -x "$VENV/bin/python" ]] || uv venv --python 3.11 "$VENV"
uv pip install --python "$VENV/bin/python" -r "$HERE/requirements.txt"

render_plists "$AGENTS"
for label in "${LABELS[@]}"; do
    plutil -lint "$AGENTS/$label.plist" >/dev/null
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # bootout is asynchronous: wait until the job is really gone, or bootstrap fails with I/O error 5.
    for _ in $(seq 1 10); do
        launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
        sleep 1
    done
done
# Both jobs are stopped, so nothing writes these now: start over if a crash loop grew one past 10 MB.
for log in houston.stderr.log whisper.stderr.log; do
    if [[ -f "$LOGS/$log" ]] && (( $(stat -f %z "$LOGS/$log") > LOG_MAX_BYTES )); then
        : > "$LOGS/$log"
        echo "Truncated $LOGS/$log (over 10 MB)."
    fi
done
for label in "${LABELS[@]}"; do
    for attempt in 1 2 3 4 5; do
        launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist" && break
        [[ $attempt -lt 5 ]] || die "launchctl bootstrap failed for $label"
        sleep 1
    done
done
echo "Houston installed. Code edits go live after re-running: make houston-install"
echo "Logs: ~/Library/Logs/houston.log"
