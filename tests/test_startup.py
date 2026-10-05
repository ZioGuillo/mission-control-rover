from unittest.mock import patch, call

import pytest

from app.main import _WIGGLE_FLAG


@pytest.fixture(autouse=True)
def _clear_wiggle_flag():
    """The flag file persists across process runs by design (once per Pi boot),
    which makes it leak between test runs unless cleared explicitly."""
    _WIGGLE_FLAG.unlink(missing_ok=True)
    yield
    _WIGGLE_FLAG.unlink(missing_ok=True)


def test_wiggle_fires_when_hardware_available():
    with patch("app.hardware.rrb3_driver.available", True), \
         patch("app.hardware.rrb3_driver.set_motors") as mock_motors, \
         patch("app.hardware.servo_driver.init"), \
         patch("app.hardware.servo_driver.available", False):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app):
            pass
    calls = mock_motors.call_args_list
    # must stop at end (last call is all zeros)
    assert calls[-1] == call(0, 0, 0, 0)
    # must have at least one left and one right move
    assert len(calls) >= 3


def test_wiggle_skipped_when_hardware_unavailable():
    with patch("app.hardware.rrb3_driver.available", False), \
         patch("app.hardware.rrb3_driver.set_motors") as mock_motors, \
         patch("app.hardware.servo_driver.init"), \
         patch("app.hardware.servo_driver.available", False):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app):
            pass
    mock_motors.assert_not_called()


def test_servo_init_runs_on_startup_even_though_available_starts_false():
    """Regression test: servo_driver.available starts False and is only set
    True *inside* init() on success, so gating the init() call on `available`
    (as main.py used to do) meant init() could never run — the servo would
    never actually initialize on real hardware."""
    with patch("app.hardware.rrb3_driver.available", False), \
         patch("app.hardware.servo_driver.available", False), \
         patch("app.hardware.servo_driver.init") as mock_init:
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app):
            pass
    mock_init.assert_called_once()
