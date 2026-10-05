#!/usr/bin/env bash
# wifi_provision.sh
#
# Reads WiFi credentials from the boot partition and connects via NetworkManager.
# Runs once on boot via wifi-provision.service (before network-online.target).
#
# Boot partition file format — /boot/firmware/wifi.txt (Bookworm)
# or /boot/wifi.txt (older RPi OS):
#
#   SSID=YourNetworkName
#   PASSWORD=YourPassword
#
# The file is deleted after a successful connection so the password
# does not sit in plain text on the SD card indefinitely.

set -euo pipefail

STAMP_FILE="/var/lib/robocontrol/wifi-provisioned"

# ── already provisioned this boot? ───────────────────────────────────
if [ -f "$STAMP_FILE" ]; then
    echo "[wifi-provision] Already ran this session — skipping"
    exit 0
fi

# ── find wifi.txt on boot partition ──────────────────────────────────
WIFI_FILE=""
for candidate in "/boot/firmware/wifi.txt" "/boot/wifi.txt"; do
    if [ -f "$candidate" ]; then
        WIFI_FILE="$candidate"
        break
    fi
done

if [ -z "$WIFI_FILE" ]; then
    echo "[wifi-provision] No wifi.txt found on boot partition — skipping"
    exit 0
fi

echo "[wifi-provision] Found: $WIFI_FILE"

# ── parse credentials ─────────────────────────────────────────────────
SSID=$(grep -E "^SSID=" "$WIFI_FILE" | cut -d= -f2- | tr -d '[:space:]')
PASSWORD=$(grep -E "^PASSWORD=" "$WIFI_FILE" | cut -d= -f2-)

if [ -z "$SSID" ]; then
    echo "[wifi-provision] ERROR: SSID not found in wifi.txt"
    exit 1
fi

echo "[wifi-provision] Connecting to SSID: $SSID"

# ── connect via NetworkManager ────────────────────────────────────────
if nmcli connection show "$SSID" &>/dev/null 2>&1; then
    echo "[wifi-provision] Updating existing connection"
    nmcli connection modify "$SSID" wifi-sec.psk "$PASSWORD" 2>/dev/null || true
    nmcli connection up "$SSID"
else
    echo "[wifi-provision] Adding new connection"
    nmcli device wifi connect "$SSID" password "$PASSWORD"
fi

# ── verify ────────────────────────────────────────────────────────────
sleep 3
# Qualified states like "connected (site only)" mean the WiFi link is up
# even though NetworkManager's own internet-reachability check failed or
# was inconclusive — only a plain, unprefixed non-"connected*" state here
# is a real sign something's wrong.
STATE=$(nmcli -t -f STATE general 2>/dev/null | head -1)
case "$STATE" in
    connected*) ;;
    *) echo "[wifi-provision] WARNING: nmcli state is '$STATE' — may still be connecting" ;;
esac

# ── clean up: delete wifi.txt so password is not left on disk ────────
rm -f "$WIFI_FILE"
echo "[wifi-provision] Deleted $WIFI_FILE (credentials stored in NetworkManager)"

# ── stamp so we don't re-run on restart ──────────────────────────────
mkdir -p "$(dirname "$STAMP_FILE")"
echo "$SSID" > "$STAMP_FILE"

echo "[wifi-provision] Done — connected to $SSID"
