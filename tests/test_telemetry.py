from unittest.mock import patch
import app.telemetry as tel


def _reset():
    tel._state.update({
        "commands_sent": 0,
        "obstacles_detected": 0,
        "last_obstacle_cm": None,
        "last_command_ms": None,
        "_motor_speed_seconds": 0.0,
    })


def test_telemetry_endpoint_returns_200(client):
    r = client.get("/api/telemetry")
    assert r.status_code == 200


def test_telemetry_has_required_keys(client):
    data = client.get("/api/telemetry").json()
    for key in ("uptime_seconds", "commands_sent", "obstacles_detected",
                "last_command_ms", "estimated_distance_m", "battery_v",
                "battery_motor_ok", "battery_pi_ok"):
        assert key in data, f"missing key: {key}"


def test_battery_v_is_none_without_hardware(client):
    data = client.get("/api/telemetry").json()
    assert data["battery_v"] is None


def test_battery_motor_ok_false_without_hardware(client):
    data = client.get("/api/telemetry").json()
    assert data["battery_motor_ok"] is False


def test_battery_pi_ok_always_true(client):
    data = client.get("/api/telemetry").json()
    assert data["battery_pi_ok"] is True


def test_record_command_increments_count():
    _reset()
    tel.record_command(10.0, "forward", 0.8)
    assert tel._state["commands_sent"] == 1
    assert tel._state["last_command_ms"] == 10.0


def test_record_obstacle_increments_and_stores_distance():
    _reset()
    tel.record_obstacle(14.5)
    assert tel._state["obstacles_detected"] == 1
    assert tel._state["last_obstacle_cm"] == 14.5


def test_distance_accumulates_from_forward_commands():
    _reset()
    tel.record_command(5.0, "forward", 1.0)
    # 1.0 speed × 0.2s pulse × 30 cm/s = 6.0 cm = 0.06 m
    assert abs(tel.snapshot()["estimated_distance_m"] - 0.06) < 0.001


def test_stop_does_not_add_distance():
    _reset()
    tel.record_command(5.0, "stop", 0.0)
    assert tel.snapshot()["estimated_distance_m"] == 0.0


def test_motor_forward_records_telemetry_via_route(client):
    _reset()
    with patch("app.routes.motors.driver.available", True), \
         patch("app.routes.motors.driver.get_distance", return_value=float("inf")), \
         patch("app.routes.motors.driver.set_motors"):
        client.post("/api/motors/forward", json={"speed": 0.5})
    data = client.get("/api/telemetry").json()
    assert data["commands_sent"] >= 1
    assert data["last_command_ms"] is not None
