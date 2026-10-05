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

# ── Python virtual environment ────────────────────────────────────────
echo "→ Creating virtual environment"
python3 -m venv "$REPO_DIR/venv"
source "$REPO_DIR/venv/bin/activate"

echo "→ Installing Python dependencies"
pip install --upgrade pip -q
pip install -r "$REPO_DIR/requirements.txt" -q

# ── GPIO backend — auto-detected from /proc/device-tree/model ─────────
# app/hardware/gpio_compat.py picks whichever of these is importable at
# runtime, so installing the right one here is the only board-specific step.
case "$BOARD_MODEL" in
    *"Raspberry Pi 5"*)
        echo "→ Detected Raspberry Pi 5 — installing rpi-lgpio"
        pip install -q "rpi-lgpio>=0.6"
        ;;
    *"Raspberry Pi"*)
        echo "→ Detected Raspberry Pi (3/4) — installing RPi.GPIO"
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
        ;;
    *)
        echo "→ Not a Raspberry Pi — skipping picamera2"
        echo "  If this board has a camera, install the OpenCV fallback instead:"
        echo "    pip install opencv-python-headless"
        ;;
esac

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

# ── robocontrol systemd service ───────────────────────────────────────
echo "→ Installing robocontrol.service"
SERVICE_SRC="$REPO_DIR/scripts/robocontrol.service"
SERVICE_DST=/etc/systemd/system/robocontrol.service
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo sed -i "s|/home/pi/robocontrol|$REPO_DIR|g" "$SERVICE_DST"
sudo sed -i "s|User=pi|User=$CURRENT_USER|g"      "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable robocontrol
sudo systemctl start  robocontrol

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
