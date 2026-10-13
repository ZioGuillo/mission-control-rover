#!/usr/bin/env bash
# jetson-fan.sh
#
# Fan control for the Jetson Nano's 4-pin fan header (J15), run by
# jetson-fan.service. Two modes, picked with FAN_MODE in .env:
#
#   1 / auto    (default) — the kernel controls the fan: off while the board
#               is cool, spinning up on its own as it heats past ~50 °C.
#               Quiet, and the lowest power draw on battery.
#   2 / always  — always spinning gently: FAN_MIN_PWM at or below
#               FAN_TEMP_LOW °C, rising linearly to full speed at
#               FAN_TEMP_HIGH °C.
#
# Switch modes with: scripts/fan-mode.sh 1|2
#
# Use this header, not a fan port on the case: the 40-pin header's 5V pins
# are wired straight to the power input, so a fan fed from them keeps
# spinning while the Jetson is off.
set -u

FAN_DIR="${FAN_SYSFS:-/sys/devices/pwm-fan}"
THERMAL_GLOB="${THERMAL_GLOB:-/sys/class/thermal/thermal_zone*}"
MODE="${FAN_MODE:-auto}"
MIN_PWM="${FAN_MIN_PWM:-70}"      # out of 255 — gentle, but enough to keep turning
TEMP_LOW="${FAN_TEMP_LOW:-40}"
TEMP_HIGH="${FAN_TEMP_HIGH:-65}"
INTERVAL="${FAN_INTERVAL:-2}"
MAX_STEP_DOWN="${FAN_MAX_STEP_DOWN:-15}"  # slow down gradually; speed up at once

case "$MODE" in
    1|auto)   MODE=auto ;;
    2|always) MODE=always ;;
    *) echo "[jetson-fan] unknown FAN_MODE='$MODE' — using auto"; MODE=auto ;;
esac

if [ ! -w "$FAN_DIR/target_pwm" ]; then
    echo "[jetson-fan] $FAN_DIR/target_pwm is missing or not writable — no Jetson fan header here, nothing to do"
    exit 0
fi

kernel_control() {
    echo 1 > "$FAN_DIR/temp_control"
}

if [ "$MODE" = auto ]; then
    kernel_control
    echo "[jetson-fan] mode 1 (auto): the kernel controls the fan by temperature"
    [ "${FAN_ONCE:-0}" = 1 ] && exit 0
    exec sleep infinity   # stay active so `systemctl status` shows the mode
fi

# Hottest CPU/GPU sensor, in °C. PMIC-Die reports a fixed value and the WiFi
# card's sensor isn't the board, so both are skipped.
board_temp() {
    local max=0 z v
    for z in $THERMAL_GLOB; do
        case "$(cat "$z/type" 2>/dev/null)" in
            CPU-therm|GPU-therm) ;;
            *) continue ;;
        esac
        v=$(( $(cat "$z/temp") / 1000 ))
        [ "$v" -gt "$max" ] && max=$v
    done
    echo "$max"
}

pwm_for() {
    local t=$1
    if [ "$t" -le "$TEMP_LOW" ]; then
        echo "$MIN_PWM"
    elif [ "$t" -ge "$TEMP_HIGH" ]; then
        echo 255
    else
        echo $(( MIN_PWM + (255 - MIN_PWM) * (t - TEMP_LOW) / (TEMP_HIGH - TEMP_LOW) ))
    fi
}

# Hand the fan back to the kernel whenever this stops, so it's never left
# fixed at a low speed on a hot board.
trap 'kernel_control; exit 0' TERM INT
echo 0 > "$FAN_DIR/temp_control"
echo "[jetson-fan] mode 2 (always): ${MIN_PWM}/255 at <=${TEMP_LOW}°C, full speed at >=${TEMP_HIGH}°C"

cur=$(cat "$FAN_DIR/target_pwm" 2>/dev/null || echo 0)
while true; do
    want=$(pwm_for "$(board_temp)")
    if [ "$want" -ge "$cur" ]; then
        cur=$want
    else
        cur=$(( cur - MAX_STEP_DOWN < want ? want : cur - MAX_STEP_DOWN ))
    fi
    echo "$cur" > "$FAN_DIR/target_pwm"
    [ "${FAN_ONCE:-0}" = 1 ] && break
    sleep "$INTERVAL" & wait $!
done
