#!/usr/bin/env bash
# install.sh
#
# Run once on the Raspberry Pi/Jetson to set up the venv, systemd services,
# WiFi provisioning, and Cloudflare Tunnel auto-assignment.
#
# Usage: cd ~/robocontrol && bash scripts/install.sh
#
# Before running, place on the boot partition:
#
#   /boot/firmware/wifi.txt          (WiFi credentials)
#     SSID=YourNetwork
#     PASSWORD=YourPassword
#
#   /boot/firmware/cloudflared.env   (Cloudflare credentials — new rover only)
#     CF_API_TOKEN=...
#     CF_ACCOUNT_ID=...
#     CF_ZONE_ID=...
#
# If this Pi already has a Cloudflare tunnel configured (~/.cloudflared/config.yml),
# the provisioning step is skipped automatically.
#
# No wifi.txt, and no network the device already knows? Every boot, after a
# short wait, wifi-connect-fallback.service opens its own "RoboCar-Setup"
# WiFi network with a captive portal to pick the real one — see README →
# "WiFi setup without a screen". Set PORTAL_SSID / PORTAL_PASSPHRASE in
# .env to customize it.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CURRENT_USER="$(whoami)"
ARCH="$(uname -m)"
BOARD_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"

echo "=== RoboControl install ==="
echo "Repo  : $REPO_DIR"
echo "User  : $CURRENT_USER"
echo "Arch  : $ARCH"
echo "Board : $BOARD_MODEL"
echo ""

# systemd runs these scripts directly, so they must be executable — copies
# made through iCloud, zip files or some scp setups drop the exec bit.
chmod +x "$REPO_DIR"/scripts/*.sh "$REPO_DIR"/scripts/*.py 2>/dev/null || true

# ── Python interpreter selection ───────────────────────────────────────
# This project requires Python 3.10+ (pyproject.toml). Raspberry Pi OS
# Bookworm and JetPack 6 (Ubuntu 22.04) ship this as `python3` already.
# JetPack 4.6 — the original Jetson Nano's last supported release, Ubuntu
# 18.04 — ships Python 3.6 as `python3`, which is too old: pip would later
# fail resolving numpy>=1.26 / pydantic-settings>=2.2 against it. Rather
# than creating a venv that breaks two steps later, look for a newer
# interpreter first (already on PATH, or installed via pyenv) and fail
# clearly with instructions if there isn't one.
find_python() {
    for candidate in python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" &>/dev/null; then
            ver="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
            major="${ver%%.*}"; minor="${ver##*.}"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    # A pyenv install isn't on PATH in a non-interactive shell unless its
    # init lines ran — check its version directory directly as a fallback.
    if [ -d "$HOME/.pyenv/versions" ]; then
        newest="$(ls "$HOME/.pyenv/versions" 2>/dev/null | sort -V | tail -1)"
        if [ -n "$newest" ] && [ -x "$HOME/.pyenv/versions/$newest/bin/python3" ]; then
            echo "$HOME/.pyenv/versions/$newest/bin/python3"
            return 0
        fi
    fi
    return 1
}

echo "→ Looking for a Python 3.10+ interpreter"
if ! PYTHON_BIN="$(find_python)"; then
    echo ""
    echo "ERROR: no Python 3.10+ interpreter found (system python3 is $(python3 --version 2>&1))."
    echo ""
    echo "  Don't replace /usr/bin/python3 to fix this — apt and other system"
    echo "  tools on Ubuntu 18.04/20.04 depend on it staying at its original"
    echo "  version. Install a newer interpreter alongside it instead:"
    echo ""
    echo "    curl https://pyenv.run | bash"
    echo "    # add pyenv's init lines to ~/.bashrc per its own install output, then:"
    echo "    sudo apt install -y build-essential libssl-dev zlib1g-dev libbz2-dev \\"
    echo "      libreadline-dev libsqlite3-dev libffi-dev liblzma-dev"
    echo "    pyenv install 3.10.14   # compiles on-device — budget 45-90 min on a Nano"
    echo ""
    echo "  Then re-run this script. See README → 'Supported Hardware' for details."
    exit 1
fi
echo "  Using: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# ── Python virtual environment ────────────────────────────────────────
# On a Raspberry Pi the camera library (picamera2) is an apt package, so the
# venv must see system site-packages. Re-running `python -m venv` on an
# existing venv rewrites pyvenv.cfg, so the flag has to be passed every time
# or a re-install silently cuts the app off from the camera.
VENV_FLAGS=""
case "$BOARD_MODEL" in
    *"Raspberry Pi"*) VENV_FLAGS="--system-site-packages" ;;
esac
echo "→ Creating virtual environment ${VENV_FLAGS:+($VENV_FLAGS)}"
"$PYTHON_BIN" -m venv $VENV_FLAGS "$REPO_DIR/venv"
source "$REPO_DIR/venv/bin/activate"

echo "→ Installing Python dependencies"
pip install --upgrade pip -q
# ARMv6 boards (Pi Zero / Zero W / Pi 1) have no prebuilt wheels for
# opencv-python-headless: pip would try to compile it for hours and fail.
# It's only the USB-webcam backend — CSI cameras there use picamera2.
if [ "$ARCH" = "armv6l" ]; then
    echo "  ARMv6 board — skipping opencv-python-headless (no ARMv6 wheels; CSI camera uses picamera2)"
    REQ_FILE="$(mktemp)"
    grep -viE '^opencv-python' "$REPO_DIR/requirements.txt" > "$REQ_FILE"
    pip install -r "$REQ_FILE" -q
    rm -f "$REQ_FILE"
else
    pip install -r "$REPO_DIR/requirements.txt" -q
fi

# ── GPIO backend — auto-detected from /proc/device-tree/model ─────────
# app/hardware/gpio_compat.py picks whichever of these is importable at
# runtime, so installing the right one here is the only board-specific step.
case "$BOARD_MODEL" in
    *"Raspberry Pi 5"*)
        echo "→ Detected Raspberry Pi 5 — installing rpi-lgpio"
        pip install -q "rpi-lgpio>=0.6"
        ;;
    *"Raspberry Pi"*)
        echo "→ Detected Raspberry Pi (Zero/2/3/4) — installing RPi.GPIO"
        pip install -q "RPi.GPIO>=0.7.1"
        ;;
    *"Jetson"*|*"NVIDIA"*)
        echo "→ Detected Jetson board — installing Jetson.GPIO"
        pip install -q "Jetson.GPIO>=2.1.0"
        echo "  NOTE: verify the RRB3 board's header wiring against a Jetson"
        echo "  pinout diagram before powering it — see README → Hardware."
        ;;
    *)
        echo "→ Unrecognized board ('$BOARD_MODEL') — skipping GPIO backend install"
        echo "  Install one manually: pip install RPi.GPIO | rpi-lgpio | Jetson.GPIO"
        ;;
esac

# ── picamera2 (Raspberry Pi CSI camera) ────────────────────────────────
case "$BOARD_MODEL" in
    *"Raspberry Pi"*)
        echo "→ Installing picamera2 system package"
        sudo apt install -y python3-picamera2 --no-install-recommends -q 2>/dev/null || \
            echo "  (picamera2 not available — CSI camera disabled)"

        # A camera overlay forced in config.txt (e.g. dtoverlay=imx219) overrides
        # camera_auto_detect, so any other sensor — like the OV5647 in most
        # 5 MP / night-vision modules — is never found. If no camera responds
        # and a sensor is forced, drop the forced line and let the Pi detect
        # it. A forced overlay that already works is left alone.
        CONFIG_TXT=/boot/firmware/config.txt
        [ -f "$CONFIG_TXT" ] || CONFIG_TXT=/boot/config.txt
        FORCED_CAM='^dtoverlay=(imx219|imx477|imx708|imx296|imx500|ov5647|ov9281)([,:].*)?$'
        if [ -f "$CONFIG_TXT" ] && grep -qE "$FORCED_CAM" "$CONFIG_TXT"; then
            if (rpicam-hello --list-cameras 2>/dev/null || libcamera-hello --list-cameras 2>/dev/null) \
                    | grep -qE '^[0-9]+ : '; then
                echo "  CSI camera detected — keeping the camera overlay set in $CONFIG_TXT"
            else
                echo "→ No CSI camera responds and $CONFIG_TXT forces a sensor — switching to auto-detect"
                sudo cp "$CONFIG_TXT" "$CONFIG_TXT.robocontrol.bak"
                sudo sed -i -E "s/$FORCED_CAM/# & (disabled by robocontrol install: camera_auto_detect finds the sensor)/" "$CONFIG_TXT"
                grep -qE '^camera_auto_detect=1' "$CONFIG_TXT" || echo "camera_auto_detect=1" | sudo tee -a "$CONFIG_TXT" >/dev/null
                echo "  Backup: $CONFIG_TXT.robocontrol.bak — REBOOT for the camera change to take effect"
            fi
        fi
        ;;
    *)
        echo "→ Not a Raspberry Pi — skipping picamera2"
        echo "  If this board has a camera, install the OpenCV fallback instead:"
        echo "    pip install opencv-python-headless"
        ;;
esac

# ── ML inference backend (tflite-runtime vs ai-edge-litert) ────────────
# Which one works is a glibc question, not a board-name question: ai-edge-
# litert's compiled libLiteRt.so needs glibc >= 2.29. Boards still on an
# older OS generation (e.g. the original Jetson Nano's last supported
# release, JetPack 4.6 / Ubuntu 18.04 / glibc 2.27) can't load it at all —
# it fails at import time with "GLIBC_2.29 not found", which looks nothing
# like a Python-version problem despite how it's easy to misread.
# tflite-runtime works fine on older glibc, but its compiled extension is
# built against NumPy 1.x's ABI and breaks under 2.x ("_ARRAY_API not
# found") — so picking it also means pinning numpy<2 for this venv. Forcing
# that pin on every board regardless of which library they actually need
# would be wrong, so this is decided here, per-install, not in requirements.txt.
# app/hardware/ml_driver.py tries tflite-runtime first, ai-edge-litert
# second, and degrades gracefully if neither import succeeds.
GLIBC_VERSION="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
GLIBC_SUPPORTS_AI_EDGE_LITERT="$(python3 -c "
v = '$GLIBC_VERSION'.split('.')
try:
    print(1 if tuple(map(int, v[:2])) >= (2, 29) else 0)
except ValueError:
    print(0)
" 2>/dev/null || echo 0)"

if [ "$ARCH" = "armv6l" ]; then
    echo "→ ARMv6 board — no ML runtime builds exist for it; skipping (ML features stay off)"
elif [ "$GLIBC_SUPPORTS_AI_EDGE_LITERT" = "1" ]; then
    echo "→ glibc $GLIBC_VERSION (>= 2.29) — installing ai-edge-litert"
    pip install -q "ai-edge-litert>=2.1.0"
else
    echo "→ glibc $GLIBC_VERSION (< 2.29) — ai-edge-litert's compiled extension"
    echo "  needs newer glibc than this OS has. Installing tflite-runtime instead"
    echo "  (pins numpy<2 in this venv — tflite-runtime needs NumPy 1.x's ABI)."
    pip install -q "numpy<2" "tflite-runtime>=2.13.0"
fi

# ── USB webcam permissions (OpenCV/V4L2 fallback — Jetson, generic boards) ──
# /dev/video* is root:video 0660 on most distros. Without this, cv2.VideoCapture()
# fails with a silent permission error that app/hardware/camera_driver.py can't
# tell apart from "no camera attached" — the UI just shows "No camera detected"
# either way, which makes this easy to misdiagnose as a hardware problem.
if ls /dev/video* &>/dev/null; then
    if id -nG "$CURRENT_USER" | grep -qw video; then
        echo "→ $CURRENT_USER already in the 'video' group — USB webcam access OK"
    else
        echo "→ Found /dev/video* but $CURRENT_USER isn't in the 'video' group — adding"
        sudo usermod -aG video "$CURRENT_USER"
        echo "  (group membership is re-checked fresh when robocontrol.service starts"
        echo "   below, so this takes effect without needing you to log out/in)"
    fi
fi

# ── .env ──────────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "→ Creating .env from .env.example"
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
else
    echo "→ .env already exists — skipping"
fi

# ── cloudflared binary ────────────────────────────────────────────────
if ! command -v cloudflared &>/dev/null; then
    echo "→ Downloading cloudflared"
    case "$ARCH" in
        aarch64) CF_BIN="cloudflared-linux-arm64"  ;;
        armv7l)  CF_BIN="cloudflared-linux-arm"    ;;
        x86_64)  CF_BIN="cloudflared-linux-amd64"  ;;
        *)        echo "  Unknown arch $ARCH — skipping cloudflared download"
                  CF_BIN="" ;;
    esac
    if [ -n "$CF_BIN" ]; then
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/$CF_BIN"
        sudo curl -fsSL "$CF_URL" -o /usr/local/bin/cloudflared
        sudo chmod +x /usr/local/bin/cloudflared
        echo "  Installed: $(cloudflared --version)"
    fi
else
    echo "→ cloudflared already installed: $(cloudflared --version)"
fi

# ── WiFi provisioning service ─────────────────────────────────────────
echo "→ Installing wifi-provision.service"
sudo cp "$REPO_DIR/scripts/wifi-provision.service" /etc/systemd/system/wifi-provision.service
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g"  /etc/systemd/system/wifi-provision.service
sudo sed -i "s|User=pi|User=$CURRENT_USER|g"       /etc/systemd/system/wifi-provision.service
sudo mkdir -p /var/lib/robocontrol
sudo systemctl daemon-reload
sudo systemctl enable wifi-provision.service
# Run now so WiFi connects immediately if wifi.txt is present
sudo bash "$REPO_DIR/scripts/wifi_provision.sh" || echo "  (wifi_provision: no wifi.txt or already connected)"

# ── WiFi Connect fallback (captive portal, for when there's no wifi.txt
#    and no already-saved network) ─────────────────────────────────────
# https://github.com/balena-os/wifi-connect — pinned version, bump by hand.
WFC_VERSION="v4.11.84"
if ! command -v wifi-connect &>/dev/null; then
    echo "→ Installing wifi-connect $WFC_VERSION"
    case "$ARCH" in
        aarch64) WFC_TARGET="aarch64-unknown-linux-gnu"        ;;
        armv7l)  WFC_TARGET="armv7-unknown-linux-gnueabihf"    ;;
        *)        echo "  Unknown arch $ARCH — skipping wifi-connect install"
                  WFC_TARGET="" ;;
    esac
    if [ -n "$WFC_TARGET" ]; then
        WFC_BASE="https://github.com/balena-os/wifi-connect/releases/download/$WFC_VERSION"
        WFC_TMP=$(mktemp -d)
        curl -fsSL "$WFC_BASE/wifi-connect-$WFC_TARGET.tar.gz" | tar -xz -C "$WFC_TMP"
        curl -fsSL "$WFC_BASE/wifi-connect-ui.tar.gz" -o "$WFC_TMP/ui.tar.gz"
        sudo install -m 755 "$WFC_TMP/wifi-connect" /usr/local/sbin/wifi-connect
        sudo mkdir -p /usr/local/share/wifi-connect/ui
        sudo rm -rf /usr/local/share/wifi-connect/ui/*
        sudo tar -xz -C /usr/local/share/wifi-connect/ui -f "$WFC_TMP/ui.tar.gz"
        rm -rf "$WFC_TMP"
        echo "  Installed: $(wifi-connect --version)"
    fi
else
    echo "→ wifi-connect already installed: $(wifi-connect --version)"
fi

echo "→ Installing wifi-connect-fallback.service"
sudo cp "$REPO_DIR/scripts/wifi-connect-fallback.service" /etc/systemd/system/wifi-connect-fallback.service
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g" /etc/systemd/system/wifi-connect-fallback.service
sudo systemctl daemon-reload
sudo systemctl enable wifi-connect-fallback.service
echo "  (not started now — it only matters on a boot with no working network;"
echo "   you're clearly already connected if you're running this over SSH)"

# ── WiFi watchdog (reconnect when NetworkManager gives up mid-run) ─────
# wifi-connect-fallback only helps at boot. If WiFi drops later and a
# reconnect handshake times out, NetworkManager can mark the saved network
# 'no-secrets' and never retry — see scripts/wifi-watchdog.sh.
echo "→ Installing wifi-watchdog.timer"
chmod +x "$REPO_DIR/scripts/wifi-watchdog.sh"
sudo cp "$REPO_DIR/scripts/wifi-watchdog.service" /etc/systemd/system/wifi-watchdog.service
sudo cp "$REPO_DIR/scripts/wifi-watchdog.timer"   /etc/systemd/system/wifi-watchdog.timer
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g" /etc/systemd/system/wifi-watchdog.service
sudo systemctl daemon-reload
sudo systemctl enable --now wifi-watchdog.timer

# ── Cloudflare Tunnel provisioning service ────────────────────────────
echo "→ Installing cloudflared-provision.service"
sudo cp "$REPO_DIR/scripts/cloudflared-provision.service" \
        /etc/systemd/system/cloudflared-provision.service
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g"  /etc/systemd/system/cloudflared-provision.service
sudo sed -i "s|User=pi|User=$CURRENT_USER|g"       /etc/systemd/system/cloudflared-provision.service
sudo systemctl daemon-reload
sudo systemctl enable cloudflared-provision.service

# ── Run tunnel provisioning now ───────────────────────────────────────
if [ -f "$HOME/.cloudflared/config.yml" ]; then
    ROVER_ID=$(cat "$HOME/.cloudflared/rover_id" 2>/dev/null || echo "??")
    echo "→ Cloudflare tunnel already configured (rover${ROVER_ID}) — skipping"
else
    echo "→ Running Cloudflare Tunnel provisioning..."
    python3 "$REPO_DIR/scripts/cloudflared_provision.py" || \
        echo "  WARNING: tunnel provisioning failed — check credentials and run again"
fi

# ── Crash hardening ───────────────────────────────────────────────────
# A Jetson Nano on this rover once hung and then stuck at the boot logo after
# days of `pcieport 0000:00:01.0: PCIe Bus Error: severity=Corrected` spam.
# Each step below closes one way a long-running SD-card board ends up there:
# logs filling the root filesystem, the ML model exhausting RAM, or a kernel
# hang/panic that leaves the board frozen instead of rebooting.

# Cap journald so log spam can never fill the SD card.
echo "→ Capping systemd journal size (200M on disk)"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/robocontrol.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=200M
SystemMaxFileSize=50M
RuntimeMaxUse=50M
EOF
sudo systemctl restart systemd-journald

# rsyslog duplicates kernel messages into /var/log/syslog and kern.log, which
# by default only rotate on a schedule — add a size cap so a flood can't grow
# them unbounded between rotations.
if [ -f /etc/logrotate.d/rsyslog ] && ! grep -q maxsize /etc/logrotate.d/rsyslog; then
    echo "→ Adding maxsize 100M to rsyslog logrotate rules"
    sudo cp /etc/logrotate.d/rsyslog /etc/logrotate.d/rsyslog.robocontrol.bak
    sudo sed -i 's/^\(\s*\)rotate \([0-9]*\)$/\1rotate \2\n\1maxsize 100M/' /etc/logrotate.d/rsyslog
    # Backups in logrotate.d are read as config too — keep it out of there.
    sudo mv /etc/logrotate.d/rsyslog.robocontrol.bak /var/lib/robocontrol/logrotate-rsyslog.bak
fi
if [ -d /etc/cron.hourly ] && [ ! -e /etc/cron.hourly/logrotate ]; then
    # Daily rotation is too slow to catch a flood; maxsize only applies when
    # logrotate actually runs.
    sudo tee /etc/cron.hourly/logrotate >/dev/null <<'EOF'
#!/bin/sh
/usr/sbin/logrotate /etc/logrotate.conf
EOF
    sudo chmod +x /etc/cron.hourly/logrotate
fi

# Jetson only: the PCIe Bus Error spam on 00:01.0 (the M.2 slot) comes from
# link power management (ASPM) on the Tegra root port. Turning ASPM off is the
# standard fix. extlinux.conf is what the bootloader reads, so back it up and
# only append to APPEND lines — a malformed file here means no boot at all.
EXTLINUX=/boot/extlinux/extlinux.conf
case "$BOARD_MODEL" in
    *"Jetson"*|*"NVIDIA"*)
        if [ -f "$EXTLINUX" ] && ! grep -q 'pcie_aspm=off' "$EXTLINUX"; then
            echo "→ Adding pcie_aspm=off to $EXTLINUX (takes effect next reboot)"
            sudo cp "$EXTLINUX" "$EXTLINUX.robocontrol.bak"
            # Also drop `quiet` so a hang during boot shows where it stopped
            # instead of sitting silently on the NVIDIA logo.
            sudo sed -i '/^\s*APPEND/ { s/ quiet\b//; s/$/ pcie_aspm=off/ }' "$EXTLINUX"
            echo "  Backup: $EXTLINUX.robocontrol.bak"
        fi
        ;;
esac

# Jetson only: fan on the board's own 4-pin header (J15), not the case's
# fan port — the 40-pin header's 5V pins are always live while the charger
# is plugged in, so a fan there never turns off. FAN_MODE in .env picks
# auto (1, default) or always-spinning (2); see scripts/jetson-fan.sh.
if [ -e /sys/devices/pwm-fan/target_pwm ]; then
    echo "→ Installing jetson-fan.service (FAN_MODE in .env, default 1 = auto)"
    chmod +x "$REPO_DIR/scripts/jetson-fan.sh" "$REPO_DIR/scripts/fan-mode.sh"
    grep -q '^FAN_MODE=' "$REPO_DIR/.env" || printf '\nFAN_MODE=1\n' >> "$REPO_DIR/.env"
    sudo cp "$REPO_DIR/scripts/jetson-fan.service" /etc/systemd/system/jetson-fan.service
    sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g" /etc/systemd/system/jetson-fan.service
    sudo systemctl daemon-reload
    sudo systemctl enable jetson-fan.service
    sudo systemctl restart jetson-fan.service
fi

# Reboot automatically on kernel panic instead of sitting frozen.
echo "→ Setting kernel.panic=10 (auto-reboot 10s after a kernel panic)"
echo "kernel.panic = 10" | sudo tee /etc/sysctl.d/90-robocontrol.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/90-robocontrol.conf

# Hardware watchdog: systemd pets it; if the whole system hangs (not just the
# app), the SoC resets itself. Jetson (tegra_wdt) and Pi (bcm2835_wdt) both
# expose /dev/watchdog; 15s is within both boards' hardware maximum.
if [ -e /dev/watchdog ]; then
    echo "→ Enabling hardware watchdog via systemd (RuntimeWatchdogSec=15)"
    sudo mkdir -p /etc/systemd/system.conf.d
    sudo tee /etc/systemd/system.conf.d/robocontrol-watchdog.conf >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=15
ShutdownWatchdogSec=2min
EOF
    sudo systemctl daemon-reexec
else
    echo "→ No /dev/watchdog — skipping hardware watchdog"
fi

# Swap headroom for the ML model. JetPack ships zram (compressed RAM) only,
# which doesn't help once real RAM is exhausted. Skip if a swap file already
# exists (e.g. Raspberry Pi OS's dphys-swapfile) or the card is short on space.
if ! awk 'NR>1 && $2=="file"' /proc/swaps | grep -q .; then
    FREE_KB="$(df --output=avail / | tail -1)"
    if [ "$FREE_KB" -ge $((6 * 1024 * 1024)) ]; then
        echo "→ Creating 2G /swapfile"
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile >/dev/null
        sudo swapon /swapfile
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    else
        echo "→ Less than 6G free on / — skipping swap file"
    fi
else
    echo "→ Swap file already present — skipping"
fi

# ── robocontrol systemd service ───────────────────────────────────────
echo "→ Installing robocontrol.service"
SERVICE_SRC="$REPO_DIR/scripts/robocontrol.service"
SERVICE_DST=/etc/systemd/system/robocontrol.service
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g" "$SERVICE_DST"
sudo sed -i "s|User=pi|User=$CURRENT_USER|g"      "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable robocontrol
# restart, not start: on a re-install the service is already running the
# previous code, and `start` would leave it that way until the next reboot.
sudo systemctl restart robocontrol

# ── done ─────────────────────────────────────────────────────────────
echo ""
echo "=== Done ==="
systemctl status robocontrol --no-pager || true
echo ""

LOCAL_IP=$(hostname -I | awk '{print $1}')
ROVER_URL=$(cat "$HOME/.cloudflared/rover_url" 2>/dev/null || echo "")

echo "  Local URL  : http://${LOCAL_IP}:8000"
if [ -n "$ROVER_URL" ]; then
    echo "  Public URL : ${ROVER_URL}"
else
    echo "  Public URL : not configured (local-only mode)"
    echo ""
    echo "  To expose this rover to the internet, add Cloudflare credentials"
    echo "  to .env and run:  python3 scripts/cloudflared_provision.py"
    echo "  See README → 'Deploying to a New Rover' for instructions."
fi
echo ""
echo "Useful commands:"
echo "  sudo systemctl status robocontrol           # app status"
echo "  sudo systemctl status cloudflared           # tunnel status"
echo "  journalctl -u robocontrol -f                # app logs"
echo "  journalctl -u cloudflared-provision -f      # provisioning logs"
echo "  journalctl -u wifi-connect-fallback -f      # captive-portal WiFi setup logs"
echo "  sudo systemctl start wifi-connect-fallback  # force the WiFi setup portal now"
