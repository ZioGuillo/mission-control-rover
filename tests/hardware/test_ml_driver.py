from app.hardware import ml_driver


def test_available_is_bool():
    assert isinstance(ml_driver.available, bool)


def test_get_detections_returns_list():
    result = ml_driver.get_detections()
    assert isinstance(result, list)


def test_set_enabled_flips_flag():
    ml_driver.set_enabled(True)
    assert ml_driver.enabled is True
    ml_driver.set_enabled(False)
    assert ml_driver.enabled is False


def test_start_does_not_crash_when_unavailable():
    if not ml_driver.available:
        ml_driver.start()  # must not raise


def test_stop_does_not_crash():
    ml_driver.stop()  # must not raise regardless of state
