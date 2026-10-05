from unittest.mock import MagicMock, patch

import pytest
import app.hardware.rrb3_driver as driver


def test_set_motors_raises_when_unavailable():
    original = driver.available
    driver.available = False
    try:
        with pytest.raises(RuntimeError, match="rrb3 not available"):
            driver.set_motors(1.0, 0, 1.0, 0)
    finally:
        driver.available = original


def test_available_is_false_without_hardware():
    assert driver.available is False


def test_get_distance_returns_inf_when_unavailable():
    original = driver.available
    driver.available = False
    try:
        assert driver.get_distance() == float("inf")
    finally:
        driver.available = original


def test_get_battery_voltage_always_none():
    """The RRB3 board has no battery-voltage sensing circuit — this must
    stay None regardless of `available`, on any board."""
    assert driver.get_battery_voltage() is None
    original = driver.available
    driver.available = True
    try:
        assert driver.get_battery_voltage() is None
    finally:
        driver.available = original


def test_set_motors_applies_pwm_scale_and_direction_pins():
    """Regression test for the port away from the vendored rrb3 package:
    duty cycle must still be scaled by motor_voltage/battery_voltage (0.5,
    matching the previous RRB3(battery_voltage=6, motor_voltage=3) call),
    and direction pins must mirror the requested dir on *_1_PIN and its
    complement on *_2_PIN."""
    original_available = driver.available
    original_left_dir = driver._old_left_dir
    original_right_dir = driver._old_right_dir
    driver.available = True
    driver._old_left_dir = 0
    driver._old_right_dir = 0

    mock_left_pwm = MagicMock()
    mock_right_pwm = MagicMock()
    mock_gpio = MagicMock()

    with patch("app.hardware.rrb3_driver._left_pwm", mock_left_pwm), \
         patch("app.hardware.rrb3_driver._right_pwm", mock_right_pwm), \
         patch("app.hardware.rrb3_driver.GPIO", mock_gpio):
        driver.set_motors(1.0, 0, 1.0, 0)  # same dirs as current state — no stop pulse

    mock_left_pwm.ChangeDutyCycle.assert_called_with(50.0)   # 1.0 * 100 * 0.5
    mock_right_pwm.ChangeDutyCycle.assert_called_with(50.0)
    mock_gpio.output.assert_any_call(driver._LEFT_1_PIN, 0)
    mock_gpio.output.assert_any_call(driver._LEFT_2_PIN, True)   # not 0
    mock_gpio.output.assert_any_call(driver._RIGHT_1_PIN, 0)
    mock_gpio.output.assert_any_call(driver._RIGHT_2_PIN, True)

    driver.available = original_available
    driver._old_left_dir = original_left_dir
    driver._old_right_dir = original_right_dir


def test_set_motors_stops_before_reversing_direction():
    original_available = driver.available
    original_left_dir = driver._old_left_dir
    original_right_dir = driver._old_right_dir
    driver.available = True
    driver._old_left_dir = 0
    driver._old_right_dir = 0

    mock_left_pwm = MagicMock()
    mock_right_pwm = MagicMock()
    mock_gpio = MagicMock()

    with patch("app.hardware.rrb3_driver._left_pwm", mock_left_pwm), \
         patch("app.hardware.rrb3_driver._right_pwm", mock_right_pwm), \
         patch("app.hardware.rrb3_driver.GPIO", mock_gpio), \
         patch("app.hardware.rrb3_driver.time.sleep"):
        driver.set_motors(1.0, 1, 1.0, 1)  # direction flip from 0 → 1

    # first call must be an all-stop (duty cycle 0) before the real command
    first_left_call = mock_left_pwm.ChangeDutyCycle.call_args_list[0]
    assert first_left_call.args[0] == 0

    driver.available = original_available
    driver._old_left_dir = original_left_dir
    driver._old_right_dir = original_right_dir
