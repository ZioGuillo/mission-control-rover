from app.config import settings
from app.hardware.gpio_compat import GPIO, available as _has_gpio

available = False
simulated = False
_pan_pwm = None
_tilt_pwm = None
_pan_angle = 90
_tilt_angle = 90

MIN_ANGLE = 0
MAX_ANGLE = 180
STEP = 15


def _angle_to_duty(angle: float) -> float:
    return 2.5 + (angle / 180.0) * 10.0


def init(pan_pin: int, tilt_pin: int) -> None:
    global _pan_pwm, _tilt_pwm, available, simulated
    if settings.simulate:
        available = True
        simulated = True
        return
    if not _has_gpio:
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pan_pin, GPIO.OUT)
        GPIO.setup(tilt_pin, GPIO.OUT)
        _pan_pwm = GPIO.PWM(pan_pin, 50)
        _tilt_pwm = GPIO.PWM(tilt_pin, 50)
        _pan_pwm.start(_angle_to_duty(_pan_angle))
        _tilt_pwm.start(_angle_to_duty(_tilt_angle))
        available = True
    except Exception:
        available = False


def move(pan_delta: int, tilt_delta: int) -> dict:
    global _pan_angle, _tilt_angle
    if not available:
        raise RuntimeError("Servo not available")
    _pan_angle = max(MIN_ANGLE, min(MAX_ANGLE, _pan_angle + pan_delta))
    _tilt_angle = max(MIN_ANGLE, min(MAX_ANGLE, _tilt_angle + tilt_delta))
    if not simulated:
        _pan_pwm.ChangeDutyCycle(_angle_to_duty(_pan_angle))
        _tilt_pwm.ChangeDutyCycle(_angle_to_duty(_tilt_angle))
    return {"pan": _pan_angle, "tilt": _tilt_angle}


def center() -> dict:
    global _pan_angle, _tilt_angle
    if not available:
        raise RuntimeError("Servo not available")
    _pan_angle = 90
    _tilt_angle = 90
    if not simulated:
        _pan_pwm.ChangeDutyCycle(_angle_to_duty(_pan_angle))
        _tilt_pwm.ChangeDutyCycle(_angle_to_duty(_tilt_angle))
    return {"pan": _pan_angle, "tilt": _tilt_angle}
