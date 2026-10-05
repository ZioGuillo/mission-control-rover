import pytest
import app.hardware.servo_driver as driver


def test_available_is_false_without_hardware():
    assert driver.available is False


def test_move_raises_when_unavailable():
    original = driver.available
    driver.available = False
    try:
        with pytest.raises(RuntimeError, match="Servo not available"):
            driver.move(15, 0)
    finally:
        driver.available = original


def test_center_raises_when_unavailable():
    original = driver.available
    driver.available = False
    try:
        with pytest.raises(RuntimeError, match="Servo not available"):
            driver.center()
    finally:
        driver.available = original


def test_init_does_nothing_when_unavailable():
    original = driver.available
    driver.available = False
    try:
        driver.init(17, 27)  # must not raise
    finally:
        driver.available = original


def test_move_clamps_pan_angle():
    original_available = driver.available
    original_pan = driver._pan_angle
    original_tilt = driver._tilt_angle
    driver.available = True
    driver._pan_angle = 175
    driver._tilt_angle = 90

    from unittest.mock import MagicMock
    driver._pan_pwm = MagicMock()
    driver._tilt_pwm = MagicMock()

    result = driver.move(10, 0)
    assert result["pan"] == 180  # clamped at MAX_ANGLE

    driver.available = original_available
    driver._pan_angle = original_pan
    driver._tilt_angle = original_tilt
    driver._pan_pwm = None
    driver._tilt_pwm = None


def test_center_resets_to_90():
    original_available = driver.available
    driver.available = True
    driver._pan_angle = 45
    driver._tilt_angle = 135

    from unittest.mock import MagicMock
    driver._pan_pwm = MagicMock()
    driver._tilt_pwm = MagicMock()

    result = driver.center()
    assert result == {"pan": 90, "tilt": 90}

    driver.available = original_available
    driver._pan_pwm = None
    driver._tilt_pwm = None


def test_init_sets_available_false_on_gpio_error():
    import app.hardware.servo_driver as drv
    from unittest.mock import patch, MagicMock

    original_available = drv.available
    original_has_gpio = drv._has_gpio
    drv.available = True
    drv._has_gpio = True

    mock_gpio = MagicMock()
    mock_gpio.setmode.side_effect = RuntimeError("GPIO error")

    with patch("app.hardware.servo_driver.GPIO", mock_gpio, create=True):
        drv.init(17, 27)
        assert drv.available is False

    drv.available = original_available
    drv._has_gpio = original_has_gpio
