#!/usr/bin/env bash
# wifi-watchdog.sh
#
# Runs every minute via wifi-watchdog.timer. Brings WiFi back if
# NetworkManager has given up on it.
#
# Why this exists: when a reconnect's WPA handshake merely times out (router
# reboot, roaming between access points), wpa_supplicant reports it as a
# possibly-wrong key. NetworkManager then asks a secret agent for a new
# password; on a headless rover there is none, so the connection fails with
# reason 'no-secrets' and NetworkManager blocks autoconnect for it —
# permanently, until someone reconnects by hand. The rover stays up but
# unreachable. `nmcli device connect` clears that block and retries.
set -uo pipefail

WIFI_DEV="$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')"
[ -n "$WIFI_DEV" ] || exit 0

STATE="$(nmcli -t -f DEVICE,STATE device 2>/dev/null | awk -F: -v d="$WIFI_DEV" '$1==d{print $2}')"

# "connected" (also covers hotspot mode while the captive portal runs) and
# in-progress states like "connecting (...)" are left alone.
case "$STATE" in
    disconnected) ;;
    *) exit 0 ;;
esac

# The captive portal manages wlan0 itself while it's running.
if systemctl is-active --quiet wifi-connect-fallback; then
    exit 0
fi

echo "[wifi-watchdog] $WIFI_DEV is disconnected — asking NetworkManager to reconnect"
nmcli device wifi rescan ifname "$WIFI_DEV" 2>/dev/null || true
sleep 5
if nmcli device connect "$WIFI_DEV"; then
    echo "[wifi-watchdog] reconnected"
else
    echo "[wifi-watchdog] reconnect failed — will retry next run"
fi
