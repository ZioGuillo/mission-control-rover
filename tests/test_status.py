from unittest.mock import patch


def test_status_ok_when_all_available(client):
    with patch("app.routes.status.rrb3_driver.available", True), \
         patch("app.routes.status.servo_driver.available", True):
        data = client.get("/api/status").json()
        assert data["ok"] is True
        assert data["message"] == "Ready"
        assert data["servo_ok"] is True
        assert data["battery_motor_ok"] is True
        assert data["battery_pi_ok"] is True


def test_status_error_when_rrb3_unavailable(client):
    with patch("app.routes.status.rrb3_driver.available", False), \
         patch("app.routes.status.servo_driver.available", True):
        data = client.get("/api/status").json()
        assert data["ok"] is False
        assert "motor" in data["message"].lower()


def test_status_ok_when_servo_unavailable(client):
    with patch("app.routes.status.rrb3_driver.available", True), \
         patch("app.routes.status.servo_driver.available", False):
        data = client.get("/api/status").json()
        assert data["ok"] is True
        assert data["message"] == "Ready"
        assert data["servo_ok"] is False
        assert data["battery_motor_ok"] is True


def test_battery_pi_always_true(client):
    with patch("app.routes.status.rrb3_driver.available", True), \
         patch("app.routes.status.servo_driver.available", False):
        data = client.get("/api/status").json()
        assert data["battery_pi_ok"] is True


def test_battery_motor_false_when_rrb3_unavailable(client):
    with patch("app.routes.status.rrb3_driver.available", False), \
         patch("app.routes.status.servo_driver.available", False):
        data = client.get("/api/status").json()
        assert data["battery_motor_ok"] is False


def test_battery_pi_always_true_even_when_rrb3_unavailable(client):
    with patch("app.routes.status.rrb3_driver.available", False), \
         patch("app.routes.status.servo_driver.available", False):
        data = client.get("/api/status").json()
        assert data["battery_pi_ok"] is True
