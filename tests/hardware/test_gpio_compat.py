import app.hardware.gpio_compat as gpio_compat


def test_reports_no_backend_on_a_machine_without_rpi_or_jetson_gpio():
    """This suite runs on dev machines / CI, never on real Pi/Jetson hardware,
    so neither RPi.GPIO nor Jetson.GPIO is importable here."""
    assert gpio_compat.available is False
    assert gpio_compat.backend == "none"
    assert gpio_compat.GPIO is None
