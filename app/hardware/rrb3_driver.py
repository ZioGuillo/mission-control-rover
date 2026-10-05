"""
Native RRB3 (RaspiRobot Board V3) motor + sonar driver.

Reimplemented directly against app.hardware.gpio_compat instead of the
vendored `rrb3` package (github.com/simonmonk/raspirobotboard3), so the
same code runs on Jetson.GPIO-based boards too — as long as the RRB3 board
is wired to the same physical header positions it would use on a
Raspberry Pi. Pin map, PWM frequency and the motor-voltage/battery-voltage
scale factor below are taken from that library's source and from how this
app previously constructed it (`RRB3(battery_voltage=6, motor_voltage=3)`).

If you're moving this board to a Jetson Nano: verify the header pinout
with a multimeter/pinout diagram before powering it — the two boards'
40-pin headers match Raspberry Pi's layout in software (BCM numbering),
but that's not a substitute for physically checking your wiring.
"""
import logging
import math
import time

from app.config import settings
from app.hardware.gpio_compat import GPIO, available as _gpio_available

_log = logging.getLogger(__name__)

available = False
simulated = False

# BCM pin numbers — from raspirobotboard3/python/rrb3.py
_LEFT_PWM_PIN, _LEFT_1_PIN, _LEFT_2_PIN = 24, 17, 4
_RIGHT_PWM_PIN, _RIGHT_1_PIN, _RIGHT_2_PIN = 14, 10, 25
_TRIGGER_PIN, _ECHO_PIN = 18, 23
_PWM_FREQ_HZ = 500
_MOTOR_DELAY = 0.2  # settle time when direction flips, avoids H-bridge shoot-through
_ECHO_TIMEOUT_LOOPS = 10_000

# matches the previous RRB3(battery_voltage=6, motor_voltage=3) construction
_PWM_SCALE = 3.0 / 6.0

_left_pwm = None
_right_pwm = None
_old_left_dir = -1
_old_right_dir = -1

if settings.simulate:
    # No GPIO at all — used for `docker/` / local testing of the dashboard
    # and telemetry without real hardware. See README "Local testing
    # without hardware".
    available = True
    simulated = True
elif _gpio_available:
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(_LEFT_PWM_PIN, GPIO.OUT)
        GPIO.setup(_LEFT_1_PIN, GPIO.OUT)
        GPIO.setup(_LEFT_2_PIN, GPIO.OUT)
        GPIO.setup(_RIGHT_PWM_PIN, GPIO.OUT)
        GPIO.setup(_RIGHT_1_PIN, GPIO.OUT)
        GPIO.setup(_RIGHT_2_PIN, GPIO.OUT)
        GPIO.setup(_TRIGGER_PIN, GPIO.OUT)
        GPIO.setup(_ECHO_PIN, GPIO.IN)
        _left_pwm = GPIO.PWM(_LEFT_PWM_PIN, _PWM_FREQ_HZ)
        _right_pwm = GPIO.PWM(_RIGHT_PWM_PIN, _PWM_FREQ_HZ)
        _left_pwm.start(0)
        _right_pwm.start(0)
        available = True
    except Exception as exc:
        _log.warning("RRB3 GPIO init failed: %s", exc)
        available = False


def _set_driver_pins(left_speed: float, left_dir: int, right_speed: float, right_dir: int) -> None:
    _left_pwm.ChangeDutyCycle(left_speed * 100 * _PWM_SCALE)
    GPIO.output(_LEFT_1_PIN, left_dir)
    GPIO.output(_LEFT_2_PIN, not left_dir)
    _right_pwm.ChangeDutyCycle(right_speed * 100 * _PWM_SCALE)
    GPIO.output(_RIGHT_1_PIN, right_dir)
    GPIO.output(_RIGHT_2_PIN, not right_dir)


def set_motors(speed1: float, dir1: int, speed2: float, dir2: int) -> None:
    """speed1/dir1 drive the left motor, speed2/dir2 the right motor —
    matches the call signature used throughout app/ (e.g. app/routes/motors.py)."""
    global _old_left_dir, _old_right_dir
    if not available:
        raise RuntimeError("rrb3 not available")
    if simulated:
        _old_left_dir, _old_right_dir = dir1, dir2
        return
    if _old_left_dir != dir1 or _old_right_dir != dir2:
        _set_driver_pins(0, 0, 0, 0)  # stop between sudden direction changes
        time.sleep(_MOTOR_DELAY)
    _set_driver_pins(speed1, dir1, speed2, dir2)
    _old_left_dir, _old_right_dir = dir1, dir2


def _simulated_distance_cm() -> float:
    """Deterministic 15-150cm sine sweep (~10s period) — enough movement to
    exercise obstacle-avoidance logic and telemetry charts without hardware."""
    return 82.5 + 67.5 * math.sin(time.monotonic() * (2 * math.pi / 10.0))


def get_distance() -> float:
    """Returns sonar distance in cm. Returns inf when hardware unavailable
    or the echo pulse times out."""
    if not available:
        return float("inf")
    if simulated:
        return _simulated_distance_cm()
    try:
        GPIO.output(_TRIGGER_PIN, True)
        time.sleep(0.0001)
        GPIO.output(_TRIGGER_PIN, False)

        count = _ECHO_TIMEOUT_LOOPS
        while GPIO.input(_ECHO_PIN) != 1 and count > 0:
            count -= 1
        start = time.time()

        count = _ECHO_TIMEOUT_LOOPS
        while GPIO.input(_ECHO_PIN) != 0 and count > 0:
            count -= 1
        finish = time.time()

        return (finish - start) / 0.000058
    except Exception:
        return float("inf")


def get_battery_voltage() -> float | None:
    """The RRB3 board has no battery-voltage sensing circuit. This was
    previously calling a method (`get_battery_voltage`) that never existed
    on the underlying rrb3 library, so it always silently returned None via
    an except-Exception fallback. Kept as an explicit no-op with the same
    return type so callers (app/routes/telemetry.py) don't need to change."""
    return None
